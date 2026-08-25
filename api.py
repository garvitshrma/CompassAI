"""
===============================================================================
 api.py  —  STAGE 5: the HTTP server (FastAPI). This is the product's front door.
===============================================================================

api.py — multi-site HTTP layer for Compass.

Sites can be added three ways:
  1. Pre-committed in sites/<site_id>/  (loaded at startup)
  2. POST /register  (manual upload, admin-only)
  3. POST /auto-register  (lightweight crawl on demand, called by widget)

The auto-register path uses requests + BS4 (no Playwright) so it fits
Render's 512MB. Won't handle JS-rendered sites, but works for the
majority of server-rendered pages.

(There is in fact a FOURTH way, added later and arguably the best one:
 POST /ingest, at the bottom of this file. The visitor's own browser posts the
 page it has ALREADY rendered, so we get JavaScript-rendered content for free
 with no headless browser on the server at all.)

-------------------------------------------------------------------------------
 WHAT A "MULTI-TENANT" SERVER MEANS HERE
-------------------------------------------------------------------------------
One running process serves many different customer websites. Each has its own
index, and requests carry a `site_id` to say which one they mean. The isolation
mechanism is deliberately boring — a dict keyed by site_id, backed by one folder
per site on disk. Boring is correct: there is no query path that can read across
sites, because the search function is handed exactly one site's data.

-------------------------------------------------------------------------------
 WHY FastAPI
-------------------------------------------------------------------------------
  * Pydantic models give you request validation, type coercion and clear 422
    errors for free — you never hand-write "if 'query' not in body".
  * It generates interactive API docs automatically. Run the server and open
    http://localhost:8000/docs to click through every endpoint below.
  * It is ASGI/async-native, which matters for an I/O-bound service like this.

    read more:
      FastAPI ......... https://fastapi.tiangolo.com/
      Pydantic ........ https://docs.pydantic.dev/latest/
      ASGI ............ https://asgi.readthedocs.io/en/latest/
      HTTP status codes https://developer.mozilla.org/en-US/docs/Web/HTTP/Status

Usage:
    uvicorn api:app --reload
    (uvicorn is the ASGI server that actually listens on a port; "api:app" means
     "import api.py, find the object named app". --reload restarts on save.)
"""

# load_dotenv() reads a .env file from the project folder and copies its
# KEY=value lines into os.environ. This lets secrets (GROQ_API_KEY, ADMIN_TOKEN)
# live in a gitignored file locally, while production reads real environment
# variables set by Render. Same code, no secrets ever committed.
#
# IT IS CALLED AT THE VERY TOP, BEFORE OTHER IMPORTS, ON PURPOSE: any imported
# module that reads os.environ at import time would otherwise see an empty
# environment. Import order is load-bearing here, which is why it looks odd.
# read more: https://pypi.org/project/python-dotenv/
from dotenv import load_dotenv
load_dotenv()

import hashlib      # SHA-256, used to detect whether a page's HTML changed
import os           # environment variables
import json         # reading/writing chunks.json and meta.json
import threading    # locks, to stop two visitors triggering the same crawl twice
import time         # sleeping in the periodic re-index loop

# asynccontextmanager turns a generator function into an async context manager.
# FastAPI's `lifespan` uses it: everything before `yield` runs at startup,
# everything after runs at shutdown.
# read more: https://docs.python.org/3/library/contextlib.html#contextlib.asynccontextmanager
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
# OpenRouter is OpenAI-compatible: the chat.completions.create(...) call in
# answer.py works against it unchanged. We only swap the client + base_url.
# read more: https://openrouter.ai/docs
from openai import OpenAI  # type: ignore[import-not-found]

from retrieval import load_all_sites, embed_site, site_dir, SITES_DIR, model, load_site
from answer import answer
from indexer import index_site

# Upper bound on the HTML a browser may POST to /ingest.
# WHY: without a cap, one visitor (or one attacker) posting a 500MB page would
# exhaust a 512MB container's memory and kill the server for every other
# customer. Any endpoint that accepts user-supplied data of unbounded size needs
# a limit like this. The underscores are just Python's numeric separators for
# readability — 3_000_000 == 3000000.
MAX_INGEST_BYTES = 3_000_000   # ~3MB rendered HTML cap (Render is 512MB)

from indexer import chunk_rendered

# ============================================================================
#  STATE — the entire in-memory database of the running server.
#
#    STATE["sites"] = { "openlake.in": (chunks_list, vectors_matrix), ... }
#    STATE["llm"]   = the shared OpenRouter-backed OpenAI client
#
#  WHY A MODULE-LEVEL DICT RATHER THAN GLOBALS?
#  A dict can be MUTATED from inside a function without needing the `global`
#  keyword (`STATE["sites"] = x` mutates; `sites = x` would rebind). It also
#  keeps all mutable server state in one obvious, greppable place.
#
#  THE TRADE-OFF, STATED HONESTLY:
#  This state lives in ONE process's memory. It works because we run a single
#  uvicorn worker. Scale to multiple workers or machines and each would hold its
#  own divergent copy — at which point this must move to Redis or a real vector
#  database. That is exactly what "Supabase + pgvector" on the roadmap means.
# ============================================================================
STATE = {"sites": {}, "llm": None}

# ---- the /query answer cache ------------------------------------------------
# WHY THIS EXISTS
# Real visitor traffic is extremely repetitive: on a college-club site, "fees",
# "projects", "events" and "how to join" account for most of what anyone ever
# asks. Without a cache, every one of those visitors triggers an identical
# retrieval + LLM round trip. With it, the FIRST visitor pays the ~1s and the
# quota; everyone after gets an instant reply from RAM.
#
# This matters doubly on free LLM tiers (OpenRouter 50 req/day, Groq ~1k+/day):
# caching typically removes 70-90% of LLM calls from real traffic, which is
# often the difference between "works all month" and "dead by lunch".
#
# CACHE KEY includes len(chunks) so a re-index naturally invalidates every
# cached answer for that site — no explicit clearing logic anywhere. If the
# index changed size, old answers cannot be trusted; if it did not change,
# answers are still valid.
#
# THE CAP is a memory guard, not a correctness device. A few thousand entries
# of small dicts is nothing; an unbounded dict on a long-lived process is a
# slow leak. On overflow we drop everything rather than evicting "oldest" —
# there is no timestamp tracking, and a cold cache self-warms in seconds.
MAX_QUERY_CACHE = 2000
_query_cache = {}

# ---- hot-reload: serve fresh indexes without a restart ----------------------
# THE PROBLEM THIS SOLVES
# load_all_sites() runs ONCE at boot. Anything that changes sites/<id>/ on disk
# afterwards — rebuild_site.py, register_site.py, a hand-edited chunks.json,
# another process re-indexing — is invisible to the running server until a
# human remembers to restart it. That "invisible staleness" produced a real,
# confusing bug here: the corrected index sat on disk while the process kept
# answering from its startup snapshot, and every test of the fix passed while
# the live server stayed wrong.
#
# THE FIX: before serving a query, stat() the site's two data files and compare
# against the mtimes we loaded. Two stat calls cost ~microseconds; a mismatch
# triggers one reload (chunks + .npy straight off disk). Restarting for DATA
# changes becomes unnecessary. CODE (.py) changes still need --reload or a
# restart, which is normal Python behaviour.
#
# CONCURRENCY: worst case two threads reload simultaneously and assign the
# same freshly-read tuple twice — idempotent, no lock needed.
STATE["mtimes"] = {}


def _site_data_fresh(sid):
    """Reload one site's index from disk if its files changed since load."""
    d = site_dir(sid)
    cj, ej = d / "chunks.json", d / "embeddings.npy"
    if not (cj.exists() and ej.exists()):
        return                                  # unknown/partial site; leave as-is
    cur = (cj.stat().st_mtime, ej.stat().st_mtime)
    if STATE["mtimes"].get(sid) == cur:
        return                                  # unchanged — the hot path

    # NEVER reload while an indexer holds this site's lock. Mid-write files
    # (chunks.json saved, embeddings.npy stale) would fail the length check in
    # load_site and trigger a SECOND concurrent embed_site() alongside the
    # in-flight one — two ONNX passes racing on a 512MB container, i.e. the
    # OOM-kill behind the mystery 502s. If the lock is busy, skip silently;
    # a later request will re-stat and pick the files up once writing is done.
    lock = _get_site_lock(sid)
    if not lock.acquire(blocking=False):
        return
    try:
        # Re-stat AFTER acquiring: the writer may have finished (and touched
        # the files again) during the gap between our first stat and the lock.
        cur = (cj.stat().st_mtime, ej.stat().st_mtime)
        if STATE["mtimes"].get(sid) == cur:
            return
        STATE["sites"][sid] = load_site(sid)
        STATE["mtimes"][sid] = cur
        print(f"[hot-reload] {sid}: index refreshed from disk")
    except Exception as e:
        # Keep the in-memory copy rather than 500-ing: stale-but-working beats
        # down just because one on-disk file is mid-write or corrupt.
        print(f"[hot-reload] {sid} failed, keeping memory copy: {e}")
    finally:
        lock.release()

# ---- concurrency control ----------------------------------------------------
# THE PROBLEM: /auto-register kicks off a crawl that can take 30+ seconds. If
# ten visitors land on an unindexed site at the same moment, ten crawls start in
# parallel — ten times the bandwidth hitting the customer's server, ten times
# our memory, and ten racing writes to the same chunks.json producing corruption.
#
# THE FIX: one lock PER SITE. Different sites can index simultaneously (no
# contention between customers); the same site cannot.
#
# `_lock_guard` is a lock protecting the DICTIONARY OF LOCKS. Without it, two
# threads could both find `sid not in _indexing_locks`, both create a NEW
# Lock object, and each take their own — meaning neither is actually excluded.
# That is a genuinely subtle race, and this is the standard fix for it.
# read more: https://docs.python.org/3/library/threading.html#lock-objects
_indexing_locks = {}
_lock_guard = threading.Lock()


def normalize_site_id(raw: str) -> str:
    """
    Reduce anything URL-shaped to one canonical site key.

        "https://www.Example.com:8080/pricing?x=1"  ->  "example.com"
        "WWW.EXAMPLE.COM"                           ->  "example.com"
        "example.com"                               ->  "example.com"

    WHY THIS FUNCTION IS LOAD-BEARING
    ---------------------------------
    The widget sends `location.hostname` from whatever page the visitor is on.
    That could be any of the forms above. If we did not normalise, a site
    registered as "example.com" would appear UNREGISTERED to a visitor who
    arrived via "www.example.com" — the classic multi-tenant lookup bug.

    Every entry point in this file (query, register, auto-register, ingest,
    and the origin check) funnels through this one function, so the mapping is
    guaranteed consistent. One canonicalisation function used everywhere beats
    five slightly-different inline cleanups.

    The `: str` annotations are type hints — documentation for humans and
    editors; Python does not enforce them at runtime.
    read more: https://docs.python.org/3/library/typing.html
    """
    # `(raw or "")` guards against None being passed in — None.strip() would
    # raise, while ("" ).strip() is fine. A common defensive idiom.
    s = (raw or "").strip().lower()

    # Strip the scheme. split("//") on "https://x.com" gives ["https:", "x.com"]
    # and [-1] takes the last piece. If there was no "//" the split returns a
    # one-element list and [-1] is the original string — so this is safe either
    # way, no if-statement needed.
    s = s.split("//")[-1]

    s = s.split("/")[0]      # drop any path:  "x.com/a/b" -> "x.com"
    s = s.split(":")[0]      # drop any port:  "x.com:8080" -> "x.com"

    # Treat "www.example.com" and "example.com" as the same customer, which is
    # what every site owner expects. 4 == len("www.").
    if s.startswith("www."):
        s = s[4:]
    return s


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Server startup and shutdown. Everything before `yield` runs ONCE when the
    process boots; everything after runs on graceful shutdown.

    WHY STARTUP WORK MATTERS SO MUCH HERE
    -------------------------------------
    Loading the embedding model takes about a second and loading + embedding
    every site can take several. If that happened lazily on the first request,
    the unlucky first visitor would wait many seconds. Doing it here means every
    real request hits a fully warm server.

    This replaces the older @app.on_event("startup") decorator, which is now
    deprecated. The context-manager form is better because setup and teardown
    for the same resource sit next to each other in one function.
    read more: https://fastapi.tiangolo.com/advanced/events/#lifespan
    """
    # Any OpenAI-compatible provider works here. The default is OpenRouter;
    # set LLM_BASE_URL in .env to switch providers without touching code:
    #   OpenRouter ... https://openrouter.ai/api/v1
    #   Groq ......... https://api.groq.com/openai/v1
    #   Gemini ....... https://generativelanguage.googleapis.com/v1beta/openai
    #   Ollama ....... http://localhost:11434/v1          (local, no key needed)
    #
    # KEY SELECTION — the subtle part. A custom LLM_BASE_URL means a custom
    # provider, so the key MUST come from LLM_API_KEY; falling back to an
    # OPENROUTER_API_KEY that happens to still be in .env would silently send
    # the wrong credential to Google/Groq and produce a baffling 401. The
    # OPENROUTER_API_KEY fallback is only honoured when we are actually
    # pointed at openrouter.ai.
    base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    is_local = "11434" in base_url          # Ollama: no auth at all
    if is_local:
        key = None
    elif "openrouter.ai" in base_url:
        key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    else:
        key = os.environ.get("LLM_API_KEY")

    if not key and not is_local:
        # FAIL FAST, AND LOUDLY. Raising here prevents the server from starting
        # at all. The alternative — booting fine and then 500-ing on the first
        # real query — is far worse: it turns a config mistake you would catch
        # in ten seconds at deploy time into a mystery outage in production.
        raise RuntimeError("No LLM API key found. Set LLM_API_KEY in .env "
                           "(or OPENROUTER_API_KEY when using OpenRouter).")

    print("warming embedder + loading sites...")

    # Calling model() with no intention of using the result looks pointless, but
    # it is the WARM-UP: it forces the lazy singleton in retrieval.py to load
    # the ~90MB ONNX model now, on our time, rather than on a visitor's.
    model()

    STATE["sites"] = load_all_sites()

    # Record the mtimes we just loaded so hot-reload only fires on real changes.
    for sid in STATE["sites"]:
        d = site_dir(sid)
        cj, ej = d / "chunks.json", d / "embeddings.npy"
        if cj.exists() and ej.exists():
            STATE["mtimes"][sid] = (cj.stat().st_mtime, ej.stat().st_mtime)

    # Build the provider-backed OpenAI client once. base_url is the only
    # meaningful difference between providers; answer.py's
    # `llm.chat.completions.create(...)` works unchanged because the wire
    # format is identical.
    #
    # The two custom headers are recommended by OpenRouter for app attribution;
    # every other provider simply ignores unknown headers.
    primary = OpenAI(
        api_key=key or "not-needed-for-local",
        base_url=base_url,
        default_headers={
            "HTTP-Referer": "https://compassai.example.com",
            "X-Title": "CompassAI",
        },
    )

    # ---- provider fallback chain -------------------------------------------
    # Free tiers die at the worst moment (a demo, a spike in traffic). If a
    # second OpenAI-compatible key is present we wrap both clients so answer.py
    # transparently fails over: same call, next provider. Today that means
    # Gemini (primary) -> OpenRouter free pool (backup) when the daily quota
    # runs dry; with different .env values it works for any pair.
    #
    # _ResilientLLM duck-types the ONE surface answer.py uses:
    # llm.chat.completions.create(**kw). Only rate/quota-style failures fail
    # over — auth errors and bad requests are your configuration's fault and
    # would fail identically on the next provider, so they raise immediately.
    class _ResilientLLM:
        def __init__(self, *clients):
            self._clients = clients
            self.chat = self._Chat(self._clients)

        class _Chat:
            def __init__(self, clients):
                self.completions = self._Completions(clients)

            class _Completions:
                def __init__(self, clients):
                    self._clients = clients

                def create(self, **kw):
                    last_exc = None
                    for i, client in enumerate(self._clients):
                        try:
                            return client.chat.completions.create(**kw)
                        except Exception as e:
                            last_exc = e
                            msg = str(e).lower()
                            transient = ("rate" in msg or "quota" in msg
                                         or "429" in msg or "capacity" in msg)
                            if not transient or i == len(self._clients) - 1:
                                raise
                            print(f"[llm] provider {i} failed "
                                  f"({type(e).__name__}), failing over...")
                    raise last_exc

    fallbacks = []
    fb_key = os.environ.get("OPENROUTER_API_KEY")
    if fb_key and "openrouter.ai" not in base_url:
        # Only add the backup when it is genuinely a DIFFERENT provider than
        # the primary; pointing both at openrouter would just double the 429s.
        fallbacks.append(OpenAI(
            api_key=fb_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer": "https://compassai.example.com",
                             "X-Title": "CompassAI"},
        ))
    STATE["llm"] = _ResilientLLM(primary, *fallbacks)
    print(f"[llm] primary={base_url}, fallbacks={len(fallbacks)}")

    print(f"ready. {len(STATE['sites'])} site(s): {list(STATE['sites'])}")

    # ---- periodic re-indexing ("the index re-trains itself") ---------------
    # REFRESH_HOURS in .env, default 24. Set 0 to disable. The thread is a
    # daemon: it dies automatically when the main process exits, so no cleanup
    # is needed after `yield`.
    try:
        refresh_hours = float(os.environ.get("REFRESH_HOURS", "24"))
    except ValueError:
        refresh_hours = 24.0
    if refresh_hours > 0:
        _start_refresher(refresh_hours)
    else:
        print("[refresher] disabled (REFRESH_HOURS=0)")

    # `yield` hands control to FastAPI, which now serves requests. Execution
    # resumes on the line below only when the server is shutting down.
    yield

    STATE["sites"].clear()


# Creating the app object. `lifespan=` wires in the function above; `title=`
# shows up in the auto-generated docs at /docs.
app = FastAPI(title="Compass API", lifespan=lifespan)

# ---- CORS ------------------------------------------------------------------
# CORS (Cross-Origin Resource Sharing) is a BROWSER security rule: by default,
# JavaScript running on customer-site.com is forbidden from reading a response
# from compass-api.onrender.com, because they are different origins. The browser
# blocks it unless the server explicitly opts in with response headers. This
# middleware adds those headers.
#
# Without this, the widget simply would not work — every fetch() would fail with
# a CORS error in the console. It is the number one thing that trips people up
# when building an embeddable widget.
#
# WHY allow_origins=["*"] (allow every website)?
# Because that IS the product: Compass is meant to be embedded on ANY customer's
# site, and we cannot know their domains in advance. The security consequence is
# acceptable because /query is a read-only endpoint over data that was scraped
# from a public website in the first place — there is nothing confidential to
# leak. The endpoints that DO write (register, ingest) carry their own separate
# checks (an admin token and an Origin check respectively).
# read more: https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # OPTIONS is in the list because browsers send an automatic "preflight"
    # OPTIONS request before any non-simple cross-origin POST, asking permission
    # first. Omit it and every POST fails before it is even sent.
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


# ---------- models ----------
# These Pydantic classes define the SHAPE of every request and response.
# FastAPI reads the type hints and automatically:
#   * parses and validates the incoming JSON
#   * returns a descriptive HTTP 422 if a field is missing or the wrong type
#   * coerces types where sensible ("3" -> 3)
#   * documents everything at /docs
# Declaring the contract once, in types, replaces a pile of manual validation.

class QueryIn(BaseModel):
    query: str                      # required — no default means mandatory
    # A default makes the field optional. This one exists for convenience during
    # development so you can curl /query without specifying a site.
    site_id: str = "openlake.in"


class QueryOut(BaseModel):
    """The response shape. Mirrors what answer.py returns.

    `str | None = None` means "a string, or null, defaulting to null". Every
    field except `found` is optional because the two possible responses are
    quite different: a success carries url/selector/heading/explanation, while a
    refusal carries only `reason`. Declaring the union of both keeps ONE stable
    response schema, so the widget can always just check `data.found` first.

    The `X | None` syntax requires Python 3.10+; older code writes
    Optional[str].
    read more: https://docs.python.org/3/library/stdtypes.html#types-union
    """
    found: bool
    url: str | None = None
    selector: str | None = None
    heading: str | None = None
    explanation: str | None = None
    confidence: float | None = None
    reason: str | None = None


class RegisterIn(BaseModel):
    site_id: str
    # `list[dict]` validates only that this is a list of objects, not their
    # inner shape. That is a deliberate looseness — chunk fields are checked
    # by hand inside register() so we can return a friendlier error message.
    chunks: list[dict]


class RegisterOut(BaseModel):
    site_id: str
    chunks: int
    status: str


class AutoRegisterIn(BaseModel):
    site_id: str


class AutoRegisterOut(BaseModel):
    site_id: str
    chunks: int
    status: str
    message: str          # human-readable text the widget can show directly


# ---------- endpoints ----------

@app.get("/health")
def health():
    """
    Liveness / status check.

    Every deployed service needs one. Render (and Kubernetes, and uptime
    monitors) periodically hit an endpoint like this to decide whether the
    process is alive and should keep receiving traffic. It is also the fastest
    way for YOU to confirm a deploy worked and see what is loaded.

    The dict comprehension `{sid: len(c) for sid, (c, _) in ...}` does nested
    unpacking: `.items()` yields (site_id, (chunks, vecs)) and we destructure
    the inner tuple inline. `_` is the conventional name for "a value I am
    deliberately ignoring" — here, the embedding matrix.
    read more: https://docs.python.org/3/tutorial/datastructures.html#dictionaries
    """
    return {
        "status": "ok",
        "sites": {sid: len(c) for sid, (c, _) in STATE["sites"].items()},
    }


@app.get("/sites")
def sites():
    """List every registered site id. Handy for debugging 'why does my widget
    say this site is not set up' — you can see instantly whether the id the
    widget derived matches the id the server stored."""
    return {"sites": list(STATE["sites"].keys())}


@app.post("/query", response_model=QueryOut)
def query(body: QueryIn):
    """
    THE MAIN ENDPOINT. Everything else in this file exists to make this work.

    `body: QueryIn` is the magic line: because the type is a Pydantic model,
    FastAPI knows to read the JSON request body, validate it, and hand you a
    typed object. `response_model=QueryOut` does the same in reverse, filtering
    and validating what goes out.

    Notice how THIN this function is. All the real logic lives in retrieval.py
    and answer.py. That separation is deliberate: those modules can be tested,
    and run from a script, with no web server involved at all.
    """
    sid = normalize_site_id(body.site_id)

    # `.get()` returns None for an unknown key instead of raising KeyError.
    site = STATE["sites"].get(sid)
    if site is None:
        # NOTE: this returns HTTP 200 with found=false, NOT a 404.
        # That is a deliberate API design choice. "This site is not registered"
        # is a normal, expected business outcome, not an HTTP-level error — and
        # keeping one consistent response shape means the widget's JavaScript
        # only ever needs one code path (`if (!data.found)`), never a mix of
        # status-code checks and body checks.
        #
        # The exact substring "not registered" is depended upon by the widget,
        # which uses it to show a different message. That is a small piece of
        # coupling worth being aware of.
        return {"found": False,
                "reason": f"site '{sid}' is not registered with Compass",
                "url": None, "selector": None, "heading": None,
                "explanation": None, "confidence": None}

    # Unpack the stored 2-tuple and hand this site's data — and only this site's
    # data — to the answer pipeline. This is where tenant isolation happens.
    # _site_data_fresh first: if someone re-indexed this site on disk since
    # boot (rebuild_site.py, /refresh from another process, hand edit), we
    # pick up the new index here without a restart.
    _site_data_fresh(sid)
    chunks, vecs = site

    # ---- cache lookup -----------------------------------------------------
    # Normalised the same way as the site id: case and stray whitespace do not
    # produce separate entries ("Fees" == "fees " == "fees"). len(chunks) in
    # the key makes any re-index invalidate this site's entries automatically.
    key = (sid, body.query.strip().lower(), len(chunks))
    if key in _query_cache:
        return _query_cache[key]

    result = answer(body.query, chunks, vecs, STATE["llm"])

    # Only cache answers that used the full pipeline. A refusal caused by a
    # provider outage (RateLimitError etc.) must NOT be cached, or the site
    # would keep refusing after the quota resets until restart. Refusals that
    # are genuinely about retrieval (score below floor) are cheap to recompute,
    # so skipping them costs nothing and avoids edge cases.
    if result.get("found") or "temporarily unavailable" not in (result.get("reason") or ""):
        if len(_query_cache) >= MAX_QUERY_CACHE:
            _query_cache.clear()
        _query_cache[key] = result
    return result


@app.post("/register", response_model=RegisterOut)
def register(body: RegisterIn, x_admin_token: str = Header(default="")):
    """
    ADMIN-ONLY bulk upload of a pre-built chunks.json.

    This is how YOU onboard a customer: run register_site.py on your laptop,
    which crawls with Playwright (needing far more than 512MB) and POSTs the
    finished chunks here. The server never runs a browser.

    THE `Header(default="")` TRICK:
    FastAPI maps the parameter name to an HTTP header by converting underscores
    to hyphens, so `x_admin_token` reads the `X-Admin-Token` header. The default
    means a missing header gives "" rather than a 422, so we can return our own
    401 below.
    read more: https://fastapi.tiangolo.com/tutorial/header-params/
    """
    expected = os.environ.get("ADMIN_TOKEN")

    # TWO conditions, and the first one matters more than it looks:
    #   `not expected` — if ADMIN_TOKEN is unset on the server, DENY everything.
    #   Without that check, an unset env var would make `expected` None, and a
    #   request that also sent nothing could compare ""==None... and more
    #   importantly a misconfigured deploy would silently become wide open.
    #   Fail CLOSED on missing configuration, always.
    if not expected or x_admin_token != expected:
        # HTTPException short-circuits the request and produces a proper HTTP
        # error response. 401 = "you are not authenticated".
        # (A production system would use secrets.compare_digest() here instead
        #  of != , to avoid a timing side-channel that can leak the token one
        #  character at a time. Worth knowing about:
        #  https://docs.python.org/3/library/secrets.html#secrets.compare_digest )
        raise HTTPException(status_code=401, detail="invalid admin token")

    sid = normalize_site_id(body.site_id)
    if not sid:
        # 400 = "your request was malformed". Distinguishing 400 from 401 from
        # 500 is how a client knows whether to fix the request, fix their
        # credentials, or retry later.
        raise HTTPException(status_code=400, detail="site_id required")
    if not body.chunks:
        raise HTTPException(status_code=400, detail="no chunks supplied")

    # ---- schema check on the chunk shape ---------------------------------
    # Pydantic validated "a list of dicts" but not what is IN them. If a chunk
    # is missing "selector", nothing fails here — it fails much later, deep
    # inside retrieval, with a confusing KeyError. Catching it at the boundary
    # with a clear message is dramatically better for whoever is debugging.
    #
    # Set difference (`required - actual`) is the neat way to express "what is
    # missing". We only check chunks[0] on the assumption that a programmatic
    # producer emits uniform records — a pragmatic 99% check for 1% of the cost.
    # read more: https://docs.python.org/3/tutorial/datastructures.html#sets
    required = {"url", "heading", "selector", "content", "level", "page_title"}
    missing = required - set(body.chunks[0].keys())
    if missing:
        # `sorted()` so the error message is deterministic — sets have no order,
        # and a message that changes between identical runs is confusing.
        raise HTTPException(status_code=400,
                            detail=f"chunks missing fields: {sorted(missing)}")

    # ---- persist, then embed, then publish -------------------------------
    d = site_dir(sid)
    # parents=True creates intermediate folders (sites/ itself, if absent);
    # exist_ok=True makes re-registering an existing site work.
    d.mkdir(parents=True, exist_ok=True)
    (d / "chunks.json").write_text(
        json.dumps(body.chunks, indent=2, ensure_ascii=False), encoding="utf-8")

    # Write to disk FIRST, then embed. Order matters: embed_site reads
    # chunks.json off the disk, and persisting first means a crash between the
    # two steps leaves recoverable data rather than nothing.
    chunks, vecs = embed_site(sid)

    # The atomic publish. Until this single assignment, /query still serves the
    # OLD index; after it, the new one. Because it is one dict assignment, no
    # request can ever observe a half-updated site.
    STATE["sites"][sid] = (chunks, vecs)
    return {"site_id": sid, "chunks": len(chunks), "status": "indexed"}


@app.post("/auto-register", response_model=AutoRegisterOut)
def auto_register(body: AutoRegisterIn):
    """
    Lightweight auto-indexing: crawl with requests + BS4 (no Playwright),
    chunk, embed, and make the site queryable — all in one request.
    Called by the widget when it lands on an unregistered site.

    THE PRODUCT IDEA: zero-touch onboarding. A site owner pastes one script tag
    and the site indexes itself on the first visit. No dashboard, no signup, no
    waiting on us. That is a real growth mechanic, not just a convenience.

    THE ENGINEERING CONSTRAINT: this runs INSIDE the 512MB API process, so it
    must use indexer.py (requests + BeautifulSoup) rather than Playwright. The
    trade-off is that JavaScript-rendered sites yield little — which is exactly
    why /ingest was later added to cover them.

    NOTE THIS ENDPOINT IS UNAUTHENTICATED, by necessity — the widget on a brand
    new site has no credentials. The abuse surface is real (anyone can make us
    crawl any domain) and the current mitigations are the per-site lock, the
    MAX_PAGES cap in indexer.py, and robots.txt compliance. Rate limiting per IP
    would be the next thing to add.
    """
    sid = normalize_site_id(body.site_id)
    if not sid:
        raise HTTPException(status_code=400, detail="site_id required")

    # FAST PATH: already indexed, so return immediately without touching a lock.
    # The widget calls this on every page load, so the overwhelming majority of
    # calls hit this branch. Making the common case cheap matters.
    if sid in STATE["sites"]:
        chunks, _ = STATE["sites"][sid]
        return {"site_id": sid, "chunks": len(chunks),
                "status": "already_indexed",
                "message": "Site is already indexed."}

    # ---- acquire this site's lock ----------------------------------------
    # dedup: if another request is already indexing this site, wait for it
    #
    # The `with _lock_guard:` block protects the dictionary itself while we
    # look up or create the per-site lock (see the note at _indexing_locks).
    # Note we get the lock object INSIDE the guard but acquire it OUTSIDE —
    # holding the global guard for the whole 30-second crawl would serialise
    # every site against every other site, defeating the point.
    with _lock_guard:
        if sid not in _indexing_locks:
            _indexing_locks[sid] = threading.Lock()
        lock = _indexing_locks[sid]

    # blocking=True + timeout=120: wait up to two minutes for whoever is already
    # crawling this site to finish, then give up. WITHOUT a timeout a stuck
    # crawl would hang every subsequent request forever, consuming a worker
    # thread each time until the server stops responding entirely.
    if not lock.acquire(blocking=True, timeout=120):
        # 503 = "service temporarily unavailable" — the semantically correct
        # code for "try again shortly", and one that well-behaved clients and
        # proxies understand as retryable.
        raise HTTPException(status_code=503,
                            detail="indexing in progress, try again shortly")

    # `try/finally` guarantees the lock is released even if the code below
    # raises. Forgetting this is the classic way to deadlock a server forever.
    try:
        # ---- DOUBLE-CHECKED LOCKING ---------------------------------------
        # double-check after acquiring lock (another thread may have finished)
        #
        # We already checked `sid in STATE["sites"]` before the lock. But we may
        # then have WAITED up to 120 seconds — during which the thread we were
        # waiting for probably finished the crawl successfully. Re-testing now
        # avoids doing the entire expensive job a second time.
        #
        # This check-lock-check pattern is a well-known concurrency idiom.
        # read more: https://en.wikipedia.org/wiki/Double-checked_locking
        if sid in STATE["sites"]:
            chunks, _ = STATE["sites"][sid]
            return {"site_id": sid, "chunks": len(chunks),
                    "status": "already_indexed",
                    "message": "Site was indexed while waiting."}

        print(f"[auto-register] crawling {sid}...")
        raw_chunks = index_site(f"https://{sid}")

        # PROTOCOL FALLBACK. We stored only the bare hostname, so we have to
        # guess the scheme. HTTPS first (correct for essentially every modern
        # site), then plain HTTP for old or internal sites that never got a
        # certificate. Trying both costs one extra failed request in the rare
        # case and saves the site from being unindexable.
        if not raw_chunks:
            # try http if https failed
            raw_chunks = index_site(f"http://{sid}")

        if not raw_chunks:
            # Note: HTTP 200 with status="failed", not an HTTP error — same
            # reasoning as in /query. This is an expected outcome that the
            # widget should display, not an exception it should catch.
            # The message names the most likely cause so the site owner knows
            # what to do next, instead of just being told "it failed".
            return {"site_id": sid, "chunks": 0,
                    "status": "failed",
                    "message": "Could not crawl the site or no content found. "
                               "The site may require JavaScript to render."}

        # save to disk so it persists across restarts
        # ---------------------------------------------------------------
        # IMPORTANT CAVEAT, worth knowing if asked: on Render's free tier the
        # filesystem is EPHEMERAL — a redeploy or a restart wipes it. So this
        # persistence is best-effort. The system self-heals anyway, because the
        # next visitor triggers auto-register (or /ingest) all over again.
        d = site_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "chunks.json").write_text(
            json.dumps(raw_chunks, indent=2, ensure_ascii=False),
            encoding="utf-8")

        # embed and load into memory
        chunks, vecs = embed_site(sid)
        STATE["sites"][sid] = (chunks, vecs)

        # A DIAGNOSTIC HINT rather than a silent partial failure. Finding fewer
        # than 5 sections almost always means the site is a JS-rendered SPA and
        # `requests` only saw the empty shell. Telling the owner that, in plain
        # language, turns a mysterious "it doesn't work" into an actionable fact.
        js_note = ""
        if len(raw_chunks) < 5:
            js_note = (" Note: very few sections found — if your site uses "
                       "JavaScript rendering, some content may be missing.")

        print(f"[auto-register] {sid} indexed: {len(chunks)} chunks")
        return {"site_id": sid, "chunks": len(chunks),
                "status": "indexed",
                "message": f"Successfully indexed {len(chunks)} sections.{js_note}"}
    finally:
        lock.release()
        # NOTE: deliberately do NOT remove the lock from _indexing_locks.
        # Popping it let a third thread create a FRESH Lock object while a
        # second thread still held a reference to the old one — two threads
        # then "excluded" each other with different locks and ran concurrent
        # embeds. The dict grows one entry per distinct site ever seen, which
        # is negligible memory; correctness beats tidiness here.


# =============================================================================
#  /refresh — PERIODIC RE-INDEXING ("the index re-trains itself")
# =============================================================================
#
#  THE PROBLEM THIS SOLVES
#  -----------------------
#  A crawled snapshot goes stale the moment the customer edits their site: a
#  new team member (a real bug we hit — a mentor existed on the live page but
#  not in our index), a changed fee, a renamed section. Until now the ONLY
#  fixes were a manual re-crawl or waiting for visitors to trigger /ingest.
#
#  Two mechanisms now exist:
#    1. POST /refresh   — admin-triggered re-crawl of one site or all sites.
#    2. The refresher thread — started in lifespan when REFRESH_HOURS > 0,
#       it calls the same core on a schedule. Default every 24h; set
#       REFRESH_HOURS=0 in .env to disable entirely.
#
#  WHY A BACKGROUND THREAD AND NOT A SYNCHRONOUS ENDPOINT: a crawl takes
#  30-120s; Render kills HTTP responses around 100s and a visitor is never
#  waiting on this. The endpoint returns "started" immediately and the work
#  happens off-thread, guarded by the same per-site locks as auto-register —
#  so a refresh can never collide with an /ingest or an auto-register for the
#  same domain.
#
#  CACHE COHERENCE FOR FREE: query-cache keys include len(chunks), so once a
#  refresh publishes a new index under the same site id, every cached answer
#  computed against the old chunk count becomes unreachable instantly. No
#  explicit invalidation needed.

def _refresh_site(sid):
    """Re-crawl + re-embed ONE site in place. Returns (status, n_chunks).

    MUST be called while holding that site's per-site lock. The publish step
    is a single dict assignment, identical to register/auto-register/ingest,
    so no reader can observe a half-updated index.
    """
    # HTTPS first, then HTTP fallback — same protocol fallback as
    # auto-register, since we store bare hostnames.
    raw = index_site(f"https://{sid}") or index_site(f"http://{sid}")
    if not raw:
        return "crawl_failed", 0

    d = site_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "chunks.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")

    chunks, vecs = embed_site(sid)
    STATE["sites"][sid] = (chunks, vecs)     # atomic publish
    return "refreshed", len(chunks)


def _refresh_sites_background(sids):
    """Spawn one daemon thread that refreshes the given sites sequentially."""
    def worker():
        for sid in sids:
            lock = _get_site_lock(sid)
            if not lock.acquire(blocking=True, timeout=300):
                print(f"[refresh] {sid}: busy (indexing in progress), skipped")
                continue
            try:
                status, n = _refresh_site(sid)
                print(f"[refresh] {sid}: {status} ({n} chunks)")
            except Exception as e:
                # One broken customer site must never stop the refresh loop
                # from reaching the others — same blast-radius reasoning as
                # load_all_sites().
                print(f"[refresh] {sid} failed: {type(e).__name__}: {e}")
            finally:
                lock.release()
                # keep the lock cached — see the note in auto_register's
                # finally block for why popping it caused a race.
    threading.Thread(target=worker, daemon=True).start()


class RefreshIn(BaseModel):
    # Optional: omit to refresh EVERY registered site.
    site_id: str | None = None


@app.post("/refresh")
def refresh(body: RefreshIn, x_admin_token: str = Header(default="")):
    """Admin-only: re-crawl + re-embed one site (or all) in the background."""
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")

    if body.site_id:
        sid = normalize_site_id(body.site_id)
        if sid not in STATE["sites"]:
            raise HTTPException(status_code=404,
                                detail=f"site '{sid}' is not registered")
        targets = [sid]
    else:
        targets = sorted(STATE["sites"].keys())
        if not targets:
            raise HTTPException(status_code=400, detail="no sites registered")

    _refresh_sites_background(targets)
    return {"status": "started", "sites": targets,
            "message": f"Re-indexing {len(targets)} site(s) in the background."}


def _start_refresher(hours):
    """Start the daemon loop that periodically refreshes every known site."""
    def loop():
        while True:
            time.sleep(hours * 3600)
            # Snapshot the key list: STATE may mutate while we work, and
            # iterating a dict while another thread inserts raises RuntimeError.
            targets = sorted(STATE["sites"].keys())
            if targets:
                print(f"[refresher] scheduled refresh of {len(targets)} site(s)")
                _refresh_sites_background(targets)
    threading.Thread(target=loop, daemon=True).start()
    print(f"[refresher] will re-index all sites every {hours}h")


# =============================================================================
#  /ingest — THE CLEVEREST ENDPOINT IN THE PROJECT. Read this section carefully.
# =============================================================================
#
#  THE INSIGHT
#  -----------
#  Crawling JavaScript-rendered sites requires running a real browser, and a
#  headless Chromium needs far more than our 512MB budget. Expensive problem.
#
#  But wait: the VISITOR'S BROWSER HAS ALREADY RENDERED THE PAGE. React has run,
#  the DOM is fully built, the content is right there. So instead of us paying
#  to render it, the widget simply POSTs `document.documentElement.outerHTML`.
#
#  We get JS-rendered content with zero rendering cost, and the index builds
#  itself incrementally as real people browse — pages nobody ever visits never
#  get indexed, which is arguably the correct prioritisation anyway.
#
#  THE COST: we are now accepting content from an untrusted client, so this
#  endpoint needs the protections that follow (origin check, size cap, hashing).

class IngestIn(BaseModel):
    site_id: str
    url: str
    title: str | None = ""     # optional; the page may have no <title>
    html: str                  # the already-rendered outerHTML


class IngestOut(BaseModel):
    site_id: str
    chunks: int
    status: str
    message: str


def _get_site_lock(sid):
    """Same per-site lock as auto-register, expressed more compactly.

    `dict.setdefault(key, default)` returns the existing value if the key is
    present, otherwise inserts the default and returns that — an atomic
    get-or-create in one call, which is exactly what we want here.
    read more: https://docs.python.org/3/library/stdtypes.html#dict.setdefault
    """
    with _lock_guard:
        return _indexing_locks.setdefault(sid, threading.Lock())


def _read_json(sid, name, default):
    """Read a JSON file from a site's folder, returning `default` on any problem.

    WHY SWALLOW THE EXCEPTION?
    Both files this reads are caches we can rebuild. If meta.json is corrupt
    (say, the process was killed mid-write) the correct behaviour is to treat it
    as empty and rebuild — not to 500 and make the site permanently unusable.
    Being lenient about recoverable state is the right call here; note we are
    NOT lenient about the request itself, which is validated strictly.
    """
    p = site_dir(sid) / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass          # fall through to the default
    return default

def _origin_ok(origin: str, sid: str) -> bool:
    """
    Anti-poisoning guard. Allow when: no Origin header (non-browser),
    local dev (localhost/127.0.0.1/file://->"null"), or origin host
    equals the site or is a subdomain of it. Not a hard boundary
    (Origin is forgeable off-browser) but blocks casual browser poisoning.

    THE ATTACK THIS PREVENTS
    ------------------------
    /ingest writes into a site's index. Without a check, anyone could POST
    {"site_id": "some-bank.com", "html": "<h1>Send money here</h1>"} and poison
    a real customer's index — visitors to that bank would then be navigated to
    attacker-authored text. This is "index poisoning".

    WHY THE `Origin` HEADER IS A MEANINGFUL SIGNAL
    ----------------------------------------------
    On any cross-origin POST the BROWSER itself stamps the Origin header with
    the page's real origin, and JavaScript running on the page CANNOT change it —
    it is on the forbidden-header list precisely so it can be trusted as a
    statement of "who is making this request".
    read more:
      https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Origin
      https://developer.mozilla.org/en-US/docs/Glossary/Forbidden_header_name

    ITS HONEST LIMIT — and note the docstring says so out loud, which is the
    right way to document a partial mitigation. `curl -H "Origin: ..."` can send
    anything. So this stops browser-based and casual poisoning, but it is not an
    authentication boundary. A real fix is a per-site key issued at signup and
    embedded in the script tag.
    """
    # No Origin at all: a server-to-server call, curl, or our own tooling. We
    # allow it because blocking would break legitimate non-browser use, and
    # anyone able to omit the header could equally forge it — so blocking would
    # add friction without adding security.
    if not origin:
        return True

    # Reuse the same canonicalisation as everywhere else, so we are comparing
    # like with like — "https://WWW.Example.com:443" becomes "example.com".
    o = normalize_site_id(origin)

    # Development allowances. "null" is what browsers send as the Origin for a
    # page opened directly from disk via file:// — that is how you would test
    # the widget against a saved HTML file locally.
    #
    # LOCALHOST POISONING GUARD: a localhost origin may only write to a
    # localhost site. Without this, the widget running on a developer's test
    # page (data-site="openlake.in" served from localhost) would happily POST
    # "I am Garvit"-style dev HTML into the REAL openlake.in production index
    # — which is exactly how a stray 'Document' chunk got in there once. The
    # reverse (localhost sid from any origin) stays allowed for local testing.
    if o in ("localhost", "127.0.0.1"):
        return sid in ("localhost", "127.0.0.1")
    if o in ("null", ""):
        return True

    # Exact match, or a subdomain. The "." in `"." + sid` is essential: without
    # it, endswith("example.com") would also accept the attacker-owned domain
    # "notexample.com". A one-character bug that would completely defeat the
    # check — this class of suffix-matching mistake is a well-known CVE pattern.
    return o == sid or o.endswith("." + sid)


@app.post("/ingest", response_model=IngestOut)
def ingest(body: IngestIn, origin: str = Header(default="")):
    """Accept one already-rendered page from a visitor's browser and index it."""
    sid = normalize_site_id(body.site_id)
    if not _origin_ok(origin, sid):
        # 403 = "I know who you are and you are not allowed", as opposed to
        # 401 = "I do not know who you are".
        raise HTTPException(status_code=403, detail="origin/site_id mismatch")

    # Anti-poisoning: the browser stamps Origin on the cross-origin POST.
    # Require it to match the site being written to. NOT a hard boundary
    # (curl can forge Origin), but it blocks browser-based / casual poisoning.

    # SIZE LIMIT. Measured in BYTES, not characters — `len(str)` counts
    # characters, and a single emoji or CJK character is 3-4 bytes in UTF-8, so
    # character-counting would let a crafted payload sneak past by ~4x. When
    # enforcing a memory limit, always measure the thing you actually care about.
    # 413 = "Payload Too Large", the correct status for exactly this.
    if len(body.html.encode("utf-8")) > MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="page too large")

    url = body.url or f"https://{sid}"

    # ---- THE CHANGE-DETECTION HASH ---------------------------------------
    # SHA-256 of the raw HTML gives us a compact fingerprint of this exact page
    # version. If the fingerprint matches what we stored last time, the page is
    # byte-identical and there is literally nothing to do — we skip parsing,
    # chunking, embedding and disk writes entirely.
    #
    # This is what makes /ingest cheap enough to call on EVERY page view. The
    # first visitor to a page pays; everyone after gets an instant no-op. It is
    # the same principle as an HTTP ETag.
    # read more:
    #   https://docs.python.org/3/library/hashlib.html
    #   https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/ETag
    page_hash = hashlib.sha256(body.html.encode("utf-8")).hexdigest()

    lock = _get_site_lock(sid)
    # 60s here rather than 120s as in auto-register: ingest does much less work
    # (one page, no network crawl), so waiting a full two minutes would mean
    # something is genuinely wrong.
    if not lock.acquire(blocking=True, timeout=60):
        raise HTTPException(status_code=503, detail="busy, retry shortly")
    try:
        # meta.json holds {"hashes": {url: sha256}} for this site.
        meta = _read_json(sid, "meta.json", {"hashes": {}})
        if meta["hashes"].get(url) == page_hash:
            cur = STATE["sites"].get(sid)
            # `len(cur[0]) if cur else 0` — a conditional expression guarding
            # against the case where the hash file survived on disk but the
            # in-memory state did not (e.g. after a restart).
            return {"site_id": sid, "chunks": len(cur[0]) if cur else 0,
                    "status": "unchanged",
                    "message": "Page already indexed at this version."}

        # chunk_rendered (in indexer.py) tries heading-based chunking first and
        # falls back to sliding text windows for heading-less SPA pages.
        page_chunks = chunk_rendered(body.html, url, body.title or "")
        if not page_chunks:
            return {"site_id": sid, "chunks": 0, "status": "empty",
                    "message": "No indexable content on this page."}

        # ---- THE MERGE: replace this page, keep every other page ----------
        # merge: replace this URL's chunks, keep the rest
        #
        # This is an UPSERT keyed on url. The list comprehension keeps every
        # chunk that came from a DIFFERENT url, and then we append this page's
        # freshly-generated chunks. Result: re-visiting a page updates it in
        # place rather than duplicating it, and the site's index grows
        # page-by-page as real visitors browse. That incremental behaviour is
        # what makes the whole zero-setup model work.
        merged = [c for c in _read_json(sid, "chunks.json", [])
                  if c.get("url") != url]
        merged.extend(page_chunks)

        # RENUMBER. Absolutely required, not cosmetic: the ids must stay equal
        # to each chunk's row index in the embeddings matrix (see the note in
        # chunker.py). Since we just removed and inserted items, every id after
        # the edit point has shifted.
        for i, c in enumerate(merged):
            c["id"] = i

        d = site_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "chunks.json").write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

        # Record the new fingerprint only AFTER the chunks were written
        # successfully. If we saved the hash first and then crashed, the page
        # would be marked "already indexed" while its content was never stored —
        # and it would never be retried. Ordering side effects so that a crash
        # leaves you retrying rather than silently skipping is a general rule.
        meta["hashes"][url] = page_hash
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        # RE-EMBED THE WHOLE SITE. Honest note on the trade-off: this is O(all
        # chunks) work for what was an O(one page) change, and on a large site
        # it is the slow part of this endpoint. It is kept because it is simple
        # and obviously correct, and because the hash check above means it only
        # runs when a page genuinely changed. The optimisation — embedding just
        # the new chunks and splicing rows into the existing matrix — is a clear
        # future improvement.
        chunks, vecs = embed_site(sid)
        STATE["sites"][sid] = (chunks, vecs)
        print(f"[ingest] {sid} {url}: +{len(page_chunks)}, {len(chunks)} total")
        return {"site_id": sid, "chunks": len(chunks), "status": "indexed",
                "message": f"Indexed this page ({len(page_chunks)} sections)."}
    finally:
        # Always, always release. See the try/finally note in auto-register.
        lock.release()
