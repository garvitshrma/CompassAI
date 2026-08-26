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

from collections import OrderedDict   # backs the bounded LRU answer cache

# asynccontextmanager turns a generator function into an async context manager.
# FastAPI's `lifespan` uses it: everything before `yield` runs at startup,
# everything after runs at shutdown.
# read more: https://docs.python.org/3/library/contextlib.html#contextlib.asynccontextmanager
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from groq import Groq

from retrieval import load_all_sites, embed_site, site_dir, SITES_DIR, model
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
#    STATE["llm"]   = the shared Groq client
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

# ---- the answer cache -------------------------------------------------------
# WHY A CACHE EARNS ITS PLACE HERE, when caches are usually premature:
#
# A navigation widget sees the SAME handful of questions over and over. Every
# visitor to a gym site asks about the coaches; every student asks about fees.
# The query distribution is tiny and extremely repetitive — which is the exact
# shape where a cache pays off enormously.
#
# It fixes both of this project's real problems at once:
#   LATENCY — a hit answers in microseconds instead of ~600ms.
#   RATE LIMITS — Groq's free tier allows 8,000 tokens per minute, and one query
#     costs ~700, so the WHOLE SERVER can serve roughly ten uncached queries a
#     minute across every customer site before it starts returning 429s. Cached
#     answers cost zero tokens and do not touch that budget at all.
#
# CORRECTNESS: the key includes site_id, so no site can ever read another's
# answer. It is invalidated on re-index (see _invalidate_cache) because a fresh
# crawl can change which selector a question should resolve to.
#
# `OrderedDict` gives O(1) move-to-end and popitem(last=False), which is all an
# LRU needs. Bounded size matters on a 512MB box: an unbounded dict keyed on
# visitor-supplied strings is a memory-exhaustion vector.
# read more: https://docs.python.org/3/library/collections.html#collections.OrderedDict
_CACHE_MAX = 500
_cache = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(sid, query):
    """Normalise so trivial variations share one entry.

    "Where are the Projects?" / "where are the projects" / "  WHERE ARE THE
    PROJECTS " all collapse to the same key. `.split()` with no argument splits
    on arbitrary runs of whitespace and drops empties, so re-joining with single
    spaces normalises internal spacing too — cheaper and more robust than a regex.
    """
    return sid, " ".join(query.lower().split())


def _cache_get(sid, query):
    with _cache_lock:
        key = _cache_key(sid, query)
        if key in _cache:
            # Mark as most-recently-used, so hot queries survive eviction.
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _cache_put(sid, query, value):
    with _cache_lock:
        _cache[_cache_key(sid, query)] = value
        # Evict oldest. last=False pops from the FRONT (least recently used).
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _invalidate_cache(sid):
    """Drop every cached answer for one site, after its index changes.

    We build the doomed key list BEFORE deleting, rather than deleting during
    iteration — mutating a dict while iterating it raises RuntimeError. This is
    the standard two-pass form.
    """
    with _cache_lock:
        for key in [k for k in _cache if k[0] == sid]:
            del _cache[key]

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
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        # FAIL FAST, AND LOUDLY. Raising here prevents the server from starting
        # at all. The alternative — booting fine and then 500-ing on the first
        # real query — is far worse: it turns a config mistake you would catch
        # in ten seconds at deploy time into a mystery outage in production.
        raise RuntimeError("Set GROQ_API_KEY before starting the server.")

    print("warming embedder + loading sites...")

    # Calling model() with no intention of using the result looks pointless, but
    # it is the WARM-UP: it forces the lazy singleton in retrieval.py to load
    # the ~90MB ONNX model now, on our time, rather than on a visitor's.
    model()

    STATE["sites"] = load_all_sites()

    # Build the Groq HTTP client once. It maintains a connection pool
    # internally, so reusing it across requests avoids re-doing TCP and TLS
    # handshakes on every single query.
    #
    # THE TIMEOUT AND RETRY SETTINGS ARE A LATENCY FIX, not boilerplate.
    # The SDK defaults to max_retries=2 with exponential backoff and a generous
    # timeout. On a free API tier — where 429 rate-limit responses are routine,
    # not exceptional — those defaults quietly turn ONE slow call into three
    # sequential attempts with waits in between. A visitor watching a spinner
    # experiences that as the widget hanging for the better part of a minute.
    #
    # A navigation widget is an interactive UI: an answer that arrives after ten
    # seconds is worth less than an honest "couldn't find that" after eight. So
    # we bound the whole thing — one retry, hard 8-second ceiling per attempt —
    # and let answer()'s except branch turn a timeout into a clean refusal.
    STATE["llm"] = Groq(api_key=key, timeout=8.0, max_retries=1)

    print(f"ready. {len(STATE['sites'])} site(s): {list(STATE['sites'])}")

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

    # CACHE LOOKUP, before any work at all. See the note at _cache: a nav widget
    # gets asked the same dozen questions endlessly, so this is a high-hit-rate
    # cache, and a hit costs no time and no Groq tokens.
    hit = _cache_get(sid, body.query)
    if hit is not None:
        return hit

    # Unpack the stored 2-tuple and hand this site's data — and only this site's
    # data — to the answer pipeline. This is where tenant isolation happens.
    chunks, vecs = site
    result = answer(body.query, chunks, vecs, STATE["llm"])

    # Only cache real verdicts. A transient failure (Groq down, rate-limited,
    # timed out) must NOT be frozen into the cache for the next 500 queries —
    # that would turn a thirty-second blip into a lasting outage for the exact
    # questions visitors ask most. Genuine refusals ARE cached: "this site has no
    # pricing page" is a stable fact about the index, not a transient error.
    if result.get("reason") != "the assistant is temporarily unavailable":
        _cache_put(sid, body.query, result)
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
    # The index changed, so previously cached answers may now point at stale
    # selectors or miss newly-added sections. Drop them.
    _invalidate_cache(sid)
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
        _invalidate_cache(sid)

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

        # Remove the lock object from the dict so it does not accumulate one
        # entry per site forever (a slow memory leak on a server that sees many
        # domains). `.pop(sid, None)` is the "delete if present, do not raise if
        # absent" form.
        with _lock_guard:
            _indexing_locks.pop(sid, None)


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
    if o in ("localhost", "127.0.0.1", "null", ""):
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
        _invalidate_cache(sid)
        print(f"[ingest] {sid} {url}: +{len(page_chunks)}, {len(chunks)} total")
        return {"site_id": sid, "chunks": len(chunks), "status": "indexed",
                "message": f"Indexed this page ({len(page_chunks)} sections)."}
    finally:
        # Always, always release. See the try/finally note in auto-register.
        lock.release()
