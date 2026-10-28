"""
===============================================================================
 navigator/views.py — the ported endpoints. Was: the bottom 700 lines of api.py.
===============================================================================

THE ONE IDEA TO TAKE FROM THIS FILE
-----------------------------------
Compare any view below with its FastAPI original and you will find the business
logic is character-for-character identical. What changed is only the three lines
at the edges: how the request body arrives, how an error is raised, and how the
response goes out. That is the whole port.

It is identical because retrieval.py, answer.py, indexer.py, site_index.py,
site_ask.py and page_query.py never imported FastAPI. They were plain Python
modules that happened to be called by a web framework. A framework you can
remove without touching your domain code is a framework you chose correctly —
and it is why "move from FastAPI to Django" is a day of work rather than a
rewrite.

THE THREE TRANSLATIONS
----------------------

  1. REQUEST BODY
       FastAPI:  def query(body: QueryIn):          # validated, typed object
       Django:   s = QueryInSerializer(data=request.data)
                 s.is_valid(raise_exception=True)
                 body = s.validated_data              # a plain dict

     Note `body` is now a DICT, so every `body.site_id` became
     `body["site_id"]`. That is the single most common edit in this file.

  2. ERRORS
       FastAPI:  raise HTTPException(status_code=400, detail="...")
       Django:   raise HttpError(400, "...")        # the small class below

     Both produce a response body of {"detail": "..."}, so clients see no
     difference.

  3. RESPONSE
       FastAPI:  return {...}                       # auto-serialised
       Django:   return Response({...})             # DRF's Response object

ONE DELIBERATE BEHAVIOUR CHANGE, FLAGGED HONESTLY
-------------------------------------------------
A malformed request body used to return HTTP 422 (FastAPI's choice) and now
returns HTTP 400 (DRF's choice). Both mean "your request was wrong", and the
widget never distinguished them — it checks `found`, not the status code. But it
IS a difference, and if you ever write a client that switches on status codes,
this is the line to remember.

 read more:
   @api_view ... https://www.django-rest-framework.org/api-guide/views/#function-based-views
   exceptions .. https://www.django-rest-framework.org/api-guide/exceptions/
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.exceptions import APIException
from rest_framework.response import Response

# The core pipeline — untouched by this port.
from core.retrieval import embed_site, site_dir
from core.answer import answer as answer_query
from core.indexer import chunk_rendered, index_site
from core.page_query import query_page
from core import site_index
from core.site_ask import ask as site_ask

from compass.boot import warm
from navigator.serializers import (
    AskInSerializer,
    AutoRegisterInSerializer,
    IngestInSerializer,
    PageQueryInSerializer,
    QueryInSerializer,
    QueryOutSerializer,
    RegisterInSerializer,
    SiteInSerializer,
)
from navigator.services import (
    normalize_site_id,
    origin_ok,
    read_site_json,
    save_site_callback,
)
from navigator.state import (
    STATE,
    cache_get,
    cache_put,
    drop_site_lock,
    get_site_lock,
    invalidate_cache,
)

log = logging.getLogger("compass")

# Upper bound on the HTML a browser may POST to /ingest.
# WHY: without a cap, one visitor posting a 500MB page would exhaust a 512MB
# container's memory and kill the server for every other customer. Any endpoint
# accepting user-supplied data of unbounded size needs a limit like this.
MAX_INGEST_BYTES = 3_000_000   # ~3MB rendered HTML cap


class HttpError(APIException):
    """A direct stand-in for FastAPI's HTTPException.

    DRF ships named exception classes — ValidationError (400),
    PermissionDenied (403), NotFound (404) — but has none for 413 or 503, and
    using the named ones would mean remembering a different class per status
    code. This one takes the status code as an argument, so the ported lines
    read almost exactly like the originals and the response body shape
    ({"detail": "..."}) is unchanged.
    """

    def __init__(self, status_code, detail):
        self.status_code = status_code
        super().__init__(detail)


def _validated(serializer_class, request):
    """Validate a request body and return it as a plain dict.

    `raise_exception=True` makes DRF raise ValidationError, which its exception
    handler turns into a 400 with a per-field error map — the equivalent of
    FastAPI's automatic 422 response.
    """
    s = serializer_class(data=request.data)
    s.is_valid(raise_exception=True)
    return s.validated_data


# ============================================================================
#  status endpoints
# ============================================================================

@api_view(["GET"])
def health(request):
    """
    Liveness / status check.

    Every deployed service needs one. Render (and uptime monitors) hit an
    endpoint like this to decide whether the process is alive and should keep
    receiving traffic. It is also the fastest way for YOU to confirm a deploy
    worked and see what is loaded.

    The dict comprehension does nested unpacking: `.items()` yields
    (site_id, (chunks, vecs)) and we destructure the inner tuple inline. `_` is
    the conventional name for a value being deliberately ignored — here, the
    embedding matrix.
    """
    warm()
    return Response({
        "status": "ok",
        "sites": {sid: len(c) for sid, (c, _) in STATE["sites"].items()},
    })


@api_view(["GET"])
def sites(request):
    """List every registered site id. Handy for debugging 'why does my widget
    say this site is not set up' — you can see instantly whether the id the
    widget derived matches the id the server stored."""
    warm()
    return Response({"sites": list(STATE["sites"].keys())})


# ============================================================================
#  /query — THE MAIN ENDPOINT. Everything else exists to make this work.
# ============================================================================

@api_view(["POST"])
def query(request):
    """
    Notice how THIN this function still is. All the real logic lives in
    retrieval.py and answer.py, which can be tested and run from a script with
    no web server involved at all. That separation is what made this port cheap.
    """
    warm()
    body = _validated(QueryInSerializer, request)
    sid = normalize_site_id(body["site_id"])

    # `.get()` returns None for an unknown key instead of raising KeyError.
    site = STATE["sites"].get(sid)
    if site is None:
        # NOTE: HTTP 200 with found=false, NOT a 404. A deliberate API design
        # choice: "this site is not registered" is a normal business outcome,
        # not an HTTP-level error — and one consistent response shape means the
        # widget's JavaScript needs one code path (`if (!data.found)`), never a
        # mix of status-code checks and body checks.
        #
        # The exact substring "not registered" is depended upon by the widget,
        # which uses it to show a different message. A small piece of coupling
        # worth being aware of.
        return Response(QueryOutSerializer({
            "found": False,
            "reason": f"site '{sid}' is not registered with Compass",
        }).data)

    # CACHE LOOKUP, before any work at all. A nav widget gets asked the same
    # dozen questions endlessly, so this is a high-hit-rate cache, and a hit
    # costs no time and no Groq tokens.
    hit = cache_get(sid, body["query"])
    if hit is not None:
        return Response(QueryOutSerializer(hit).data)

    # Unpack the stored 2-tuple and hand this site's data — and only this
    # site's data — to the answer pipeline. This is where tenant isolation
    # happens: there is no code path that can read across sites, because the
    # search function is handed exactly one site's data.
    chunks, vecs = site
    result = answer_query(body["query"], chunks, vecs, STATE["llm"])

    # Only cache real verdicts. A transient failure (Groq down, rate-limited,
    # timed out) must NOT be frozen into the cache for the next 500 queries —
    # that would turn a thirty-second blip into a lasting outage for the exact
    # questions visitors ask most. Genuine refusals ARE cached: "this site has
    # no pricing page" is a stable fact about the index, not a transient error.
    if result.get("reason") != "the assistant is temporarily unavailable":
        cache_put(sid, body["query"], result)

    return Response(QueryOutSerializer(result).data)


# ============================================================================
#  Chrome extension — /page-query, /site, /ask
# ============================================================================
# The extension works on ANY website, so instead of a site_id it sends the
# sections of the page the visitor is looking at, read in their browser.

@api_view(["POST"])
def page_query(request):
    """Answer a question about one page the extension sent. Stateless —
    nothing is stored."""
    warm()
    body = _validated(PageQueryInSerializer, request)

    if not body["question"].strip():
        raise HttpError(400, "question required")
    if len(body["sections"]) > 400:
        raise HttpError(413, "too many sections")

    # No `.model_dump()` loop needed any more: DRF's nested serializer already
    # handed us a list of plain dicts, which is what query_page() wants.
    return Response(query_page(body["question"], body["url"], body["title"],
                               list(body["sections"]), STATE["llm"]))


@api_view(["POST"])
def site(request):
    """Called when the side panel opens on a website. Starts a background crawl
    of the site's public pages if we do not have them yet (see site_index.py)
    and returns the site map for instant suggestions. The extension calls it
    again every few seconds while the state is "indexing"."""
    warm()
    body = _validated(SiteInSerializer, request)

    sid = normalize_site_id(body["url"])
    page = site_index.clean_url(body["url"])
    if not sid or not page:
        raise HttpError(400, "url required")

    parts = urlsplit(page)
    root = f"{parts.scheme}://{parts.netloc}"          # the home page

    links = [l for l in list(body["links"])[:site_index.MAX_SEEDS]
             if site_index.on_site(l["url"], sid)]
    current = STATE["sites"].get(sid)
    chunks = current[0] if current else []

    if site_index.needs_crawl(sid, chunks, links):
        if site_index.is_public_host(parts.hostname or ""):
            site_index.start(sid, root, links, chunks, save_site_callback(sid),
                             page=page)
        else:
            # Refuse to crawl private or internal hosts — an SSRF guard.
            site_index.STATUS[sid] = {"state": "failed", "pages": 0, "at": 0.0}

    st = site_index.STATUS.get(sid, {"state": "ready" if chunks else "failed"})

    # Re-read STATE: the crawl above may have completed synchronously and
    # replaced this site's entry via the save callback.
    current = STATE["sites"].get(sid)
    chunks = current[0] if current else []

    return Response({
        "site": sid,
        "state": st["state"] if st["state"] != "failed" or not chunks else "ready",
        "pages": len({c["url"] for c in chunks}),
        "sections": len(chunks),
        "map": site_index.site_map(chunks),
    })


@api_view(["POST"])
def ask(request):
    """Answer a question from the whole site plus the live page, with an
    explanation written by the LLM (see site_ask.py)."""
    warm()
    body = _validated(AskInSerializer, request)

    if not body["question"].strip():
        raise HttpError(400, "question required")
    if len(body["sections"]) > 400:
        raise HttpError(413, "too many sections")

    sid = normalize_site_id(body["url"])
    current = STATE["sites"].get(sid)
    chunks, vecs = current if current else ([], None)

    result = site_ask(body["question"], body["url"], body["title"],
                      list(body["sections"]),
                      list(body["history"])[-2:],
                      chunks, vecs, STATE["llm"],
                      intro=body["intro"][:1500],
                      page_text=body["page_text"][:6000])

    result["site_state"] = site_index.STATUS.get(sid, {}).get(
        "state", "ready" if chunks else "none")
    return Response(result)


# ============================================================================
#  /privacy — a plain HTML page, not JSON
# ============================================================================

def privacy(request):
    """The extension's privacy policy. The Chrome Web Store needs a public URL.

    WHY THIS IS NOT A DRF VIEW
    --------------------------
    FastAPI handled this with `response_class=HTMLResponse`. The DRF equivalent
    would mean writing a custom renderer, because DRF's whole job is content
    negotiation over serialised data — and this endpoint has no data, just a
    file. So this is a plain Django function view returning an HttpResponse,
    which is simpler and more honest about what it does.

    A plain view is fine here because DRF is a library, not a mode: Django
    routes to both kinds side by side in the same urls.py.

    BASE_DIR rather than Path(__file__).parent: the old code sat next to
    privacy.html at the repo root, and this file does not, so it must point at
    the project root explicitly.
    """
    html = (Path(settings.BASE_DIR) / "privacy.html").read_text(encoding="utf-8")
    return HttpResponse(html, content_type="text/html; charset=utf-8")


# ============================================================================
#  /register — admin-only bulk upload of a pre-built chunks.json
# ============================================================================

@api_view(["POST"])
def register(request):
    """
    This is how YOU onboard a customer: run register_site.py on your laptop,
    which crawls with Playwright (needing far more than 512MB) and POSTs the
    finished chunks here. The server never runs a browser.

    THE HEADER, AND WHAT CHANGED
    ----------------------------
    FastAPI used a function parameter with a bit of magic:

        def register(body: RegisterIn, x_admin_token: str = Header(default="")):

    It converted the parameter name's underscores to hyphens to find the
    `X-Admin-Token` header. Django has no such magic; you read the header
    directly off the request. Less clever, more obvious — and `request.headers`
    is case-insensitive, so "x-admin-token" would work too.
    """
    warm()
    expected = os.environ.get("ADMIN_TOKEN")
    supplied = request.headers.get("X-Admin-Token", "")

    # TWO conditions, and the first matters more than it looks:
    #   `not expected` — if ADMIN_TOKEN is unset on the server, DENY everything.
    #   Without that check a misconfigured deploy would silently become wide
    #   open. Fail CLOSED on missing configuration, always.
    #
    # (A production system would use secrets.compare_digest() instead of != to
    #  avoid a timing side-channel that can leak the token one character at a
    #  time: https://docs.python.org/3/library/secrets.html#secrets.compare_digest )
    if not expected or supplied != expected:
        raise HttpError(401, "invalid admin token")   # 401 = not authenticated

    body = _validated(RegisterInSerializer, request)

    sid = normalize_site_id(body["site_id"])
    if not sid:
        # 400 = "your request was malformed". Distinguishing 400 from 401 from
        # 500 is how a client knows whether to fix the request, fix their
        # credentials, or retry later.
        raise HttpError(400, "site_id required")
    if not body["chunks"]:
        raise HttpError(400, "no chunks supplied")

    # ---- schema check on the chunk shape ---------------------------------
    # The serializer validated "a list of dicts" but not what is IN them. If a
    # chunk is missing "selector", nothing fails here — it fails much later,
    # deep inside retrieval, with a confusing KeyError. Catching it at the
    # boundary with a clear message is dramatically better for whoever is
    # debugging.
    #
    # Set difference (`required - actual`) is the neat way to express "what is
    # missing". We check only chunks[0], on the assumption that a programmatic
    # producer emits uniform records — a pragmatic 99% check for 1% of the cost.
    required = {"url", "heading", "selector", "content", "level", "page_title"}
    missing = required - set(body["chunks"][0].keys())
    if missing:
        # `sorted()` so the message is deterministic — sets have no order, and a
        # message that changes between identical runs is confusing.
        raise HttpError(400, f"chunks missing fields: {sorted(missing)}")

    # ---- persist, then embed, then publish -------------------------------
    d = site_dir(sid)
    # parents=True creates intermediate folders (sites/ itself, if absent);
    # exist_ok=True makes re-registering an existing site work.
    d.mkdir(parents=True, exist_ok=True)
    (d / "chunks.json").write_text(
        json.dumps(body["chunks"], indent=2, ensure_ascii=False), encoding="utf-8")

    # Write to disk FIRST, then embed. Order matters: embed_site reads
    # chunks.json off the disk, and persisting first means a crash between the
    # two steps leaves recoverable data rather than nothing.
    chunks, vecs = embed_site(sid)

    # The atomic publish. Until this single assignment, /query still serves the
    # OLD index; after it, the new one. Because it is one dict assignment, no
    # request can ever observe a half-updated site.
    STATE["sites"][sid] = (chunks, vecs)

    # The index changed, so cached answers may now point at stale selectors or
    # miss newly-added sections. Drop them.
    invalidate_cache(sid)
    return Response({"site_id": sid, "chunks": len(chunks), "status": "indexed"})


# ============================================================================
#  /auto-register — zero-touch onboarding: the site indexes itself
# ============================================================================

@api_view(["POST"])
def auto_register(request):
    """
    Lightweight auto-indexing: crawl with requests + BS4 (no Playwright), chunk,
    embed, and make the site queryable — all in one request. Called by the
    widget when it lands on an unregistered site.

    THE PRODUCT IDEA: a site owner pastes one script tag and the site indexes
    itself on the first visit. No dashboard, no signup, no waiting on us. That
    is a real growth mechanic, not just a convenience.

    THE ENGINEERING CONSTRAINT: this runs INSIDE the 512MB process, so it uses
    indexer.py (requests + BeautifulSoup) rather than Playwright. The trade-off
    is that JavaScript-rendered sites yield little — which is exactly why
    /ingest was later added to cover them.

    THIS ENDPOINT IS UNAUTHENTICATED by necessity: the widget on a brand new
    site has no credentials. The abuse surface is real (anyone can make us crawl
    any domain) and the current mitigations are the per-site lock, the MAX_PAGES
    cap in indexer.py, and robots.txt compliance. Rate limiting per IP is the
    next thing to add.

    A DJANGO-SPECIFIC WARNING ABOUT THIS ENDPOINT
    ---------------------------------------------
    A crawl here can take 30+ seconds, and the lock below waits up to 120.
    gunicorn's DEFAULT worker timeout is 30 seconds, after which it KILLS the
    worker mid-crawl — the client sees a connection reset and the log shows
    "WORKER TIMEOUT" with no traceback. uvicorn had no such default, so this
    failure mode is new with the port. render.yaml therefore sets
    `--timeout 180`, and that flag is load-bearing, not decoration.
    """
    warm()
    body = _validated(AutoRegisterInSerializer, request)

    sid = normalize_site_id(body["site_id"])
    if not sid:
        raise HttpError(400, "site_id required")

    # FAST PATH: already indexed, so return immediately without touching a
    # lock. The widget calls this on every page load, so the overwhelming
    # majority of calls hit this branch. Making the common case cheap matters.
    if sid in STATE["sites"]:
        chunks, _ = STATE["sites"][sid]
        return Response({"site_id": sid, "chunks": len(chunks),
                         "status": "already_indexed",
                         "message": "Site is already indexed."})

    # Get the per-site lock. We fetch it under the dict guard but acquire it
    # outside: holding the global guard for a 30-second crawl would serialise
    # every site against every other site, defeating the point.
    lock = get_site_lock(sid)

    # blocking=True + timeout=120: wait up to two minutes for whoever is already
    # crawling this site, then give up. WITHOUT a timeout a stuck crawl would
    # hang every subsequent request forever, consuming a worker thread each time
    # until the server stops responding entirely.
    if not lock.acquire(blocking=True, timeout=120):
        # 503 = "service temporarily unavailable" — the semantically correct
        # code for "try again shortly", which well-behaved clients and proxies
        # understand as retryable.
        raise HttpError(503, "indexing in progress, try again shortly")

    # `try/finally` guarantees the lock is released even if the code below
    # raises. Forgetting this is the classic way to deadlock a server forever.
    try:
        # ---- DOUBLE-CHECKED LOCKING ---------------------------------------
        # We already checked `sid in STATE["sites"]` before the lock. But we may
        # then have WAITED up to 120 seconds — during which the thread we were
        # waiting for probably finished the crawl successfully. Re-testing now
        # avoids doing the entire expensive job a second time.
        # https://en.wikipedia.org/wiki/Double-checked_locking
        if sid in STATE["sites"]:
            chunks, _ = STATE["sites"][sid]
            return Response({"site_id": sid, "chunks": len(chunks),
                             "status": "already_indexed",
                             "message": "Site was indexed while waiting."})

        log.info("[auto-register] crawling %s...", sid)
        raw_chunks = index_site(f"https://{sid}")

        # PROTOCOL FALLBACK. We stored only the bare hostname, so we have to
        # guess the scheme. HTTPS first (correct for essentially every modern
        # site), then plain HTTP for old or internal sites without a
        # certificate. One extra failed request in the rare case, and it saves
        # the site from being unindexable.
        if not raw_chunks:
            raw_chunks = index_site(f"http://{sid}")

        if not raw_chunks:
            # HTTP 200 with status="failed", not an HTTP error — same reasoning
            # as /query. An expected outcome the widget should display, not an
            # exception it should catch. The message names the most likely cause
            # so the owner knows what to do next.
            return Response({"site_id": sid, "chunks": 0, "status": "failed",
                             "message": "Could not crawl the site or no content "
                                        "found. The site may require JavaScript "
                                        "to render."})

        # IMPORTANT CAVEAT: on Render's free tier the filesystem is EPHEMERAL —
        # a redeploy or restart wipes it. So this persistence is best-effort.
        # The system self-heals anyway, because the next visitor triggers
        # auto-register (or /ingest) all over again.
        d = site_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "chunks.json").write_text(
            json.dumps(raw_chunks, indent=2, ensure_ascii=False), encoding="utf-8")

        chunks, vecs = embed_site(sid)
        STATE["sites"][sid] = (chunks, vecs)
        invalidate_cache(sid)

        # A DIAGNOSTIC HINT rather than a silent partial failure. Fewer than
        # five sections almost always means the site is a JS-rendered SPA and
        # `requests` only saw the empty shell. Saying so in plain language turns
        # a mysterious "it doesn't work" into an actionable fact.
        js_note = ""
        if len(raw_chunks) < 5:
            js_note = (" Note: very few sections found — if your site uses "
                       "JavaScript rendering, some content may be missing.")

        log.info("[auto-register] %s indexed: %d chunks", sid, len(chunks))
        return Response({"site_id": sid, "chunks": len(chunks),
                         "status": "indexed",
                         "message": f"Successfully indexed {len(chunks)} "
                                    f"sections.{js_note}"})
    finally:
        lock.release()
        # Drop the lock object so the dict does not accumulate one entry per
        # site forever (a slow leak on a server that meets many domains).
        drop_site_lock(sid)


# ============================================================================
#  /ingest — THE CLEVEREST ENDPOINT IN THE PROJECT. Read this carefully.
# ============================================================================
#
#  THE INSIGHT
#  -----------
#  Crawling JavaScript-rendered sites requires running a real browser, and a
#  headless Chromium needs far more than our 512MB budget. Expensive problem.
#
#  But the VISITOR'S BROWSER HAS ALREADY RENDERED THE PAGE. React has run, the
#  DOM is fully built, the content is right there. So instead of us paying to
#  render it, the widget simply POSTs `document.documentElement.outerHTML`.
#
#  We get JS-rendered content at zero rendering cost, and the index builds
#  itself incrementally as real people browse — pages nobody visits never get
#  indexed, which is arguably the correct prioritisation anyway.
#
#  THE COST: we are accepting content from an untrusted client, so this endpoint
#  needs the protections below (origin check, size cap, hashing).

@api_view(["POST"])
def ingest(request):
    """Accept one already-rendered page from a visitor's browser and index it."""
    warm()
    body = _validated(IngestInSerializer, request)

    sid = normalize_site_id(body["site_id"])

    # The browser stamps Origin on a cross-origin POST and page JavaScript
    # cannot forge it. Require it to match the site being written to.
    origin = request.headers.get("Origin", "")
    if not origin_ok(origin, sid):
        # 403 = "I know who you are and you are not allowed", as opposed to
        # 401 = "I do not know who you are".
        raise HttpError(403, "origin/site_id mismatch")

    html = body["html"]

    # SIZE LIMIT, measured in BYTES not characters. `len(str)` counts
    # characters, and a single emoji or CJK character is 3-4 bytes in UTF-8, so
    # character-counting would let a crafted payload sneak past by ~4x. When
    # enforcing a memory limit, measure the thing you actually care about.
    # 413 = "Payload Too Large", the correct status for exactly this.
    if len(html.encode("utf-8")) > MAX_INGEST_BYTES:
        raise HttpError(413, "page too large")

    url = body["url"] or f"https://{sid}"

    # ---- THE CHANGE-DETECTION HASH ---------------------------------------
    # SHA-256 of the raw HTML is a compact fingerprint of this exact page
    # version. If it matches what we stored last time, the page is byte-
    # identical and there is literally nothing to do — we skip parsing,
    # chunking, embedding and disk writes entirely.
    #
    # This is what makes /ingest cheap enough to call on EVERY page view. The
    # first visitor to a page pays; everyone after gets an instant no-op. Same
    # principle as an HTTP ETag.
    #
    # THIS IS WHY `trim_whitespace=False` ON THE html FIELD MATTERS: DRF would
    # otherwise strip surrounding whitespace, changing these bytes and so
    # changing every fingerprint we have already stored.
    page_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()

    lock = get_site_lock(sid)
    # 60s rather than 120s as in auto-register: ingest does much less work (one
    # page, no network crawl), so waiting two minutes would mean something is
    # genuinely wrong.
    if not lock.acquire(blocking=True, timeout=60):
        raise HttpError(503, "busy, retry shortly")
    try:
        # meta.json holds {"hashes": {url: sha256}} for this site.
        meta = read_site_json(sid, "meta.json", {"hashes": {}})
        if meta["hashes"].get(url) == page_hash:
            cur = STATE["sites"].get(sid)
            # The conditional guards the case where the hash file survived on
            # disk but the in-memory state did not (e.g. after a restart).
            return Response({"site_id": sid,
                             "chunks": len(cur[0]) if cur else 0,
                             "status": "unchanged",
                             "message": "Page already indexed at this version."})

        # chunk_rendered tries heading-based chunking first and falls back to
        # sliding text windows for heading-less SPA pages.
        page_chunks = chunk_rendered(html, url, body["title"] or "")
        if not page_chunks:
            return Response({"site_id": sid, "chunks": 0, "status": "empty",
                             "message": "No indexable content on this page."})

        # ---- THE MERGE: replace this page, keep every other page ----------
        # An UPSERT keyed on url. The comprehension keeps every chunk that came
        # from a DIFFERENT url, then we append this page's fresh chunks. So
        # re-visiting a page updates it in place rather than duplicating it, and
        # the site's index grows page-by-page as real visitors browse. That
        # incremental behaviour is what makes the whole zero-setup model work.
        merged = [c for c in read_site_json(sid, "chunks.json", [])
                  if c.get("url") != url]
        merged.extend(page_chunks)

        # RENUMBER. Required, not cosmetic: each id must stay equal to that
        # chunk's ROW INDEX in the embeddings matrix (see the note in
        # chunker.py). We just removed and inserted items, so every id after the
        # edit point has shifted.
        for i, c in enumerate(merged):
            c["id"] = i

        d = site_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "chunks.json").write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

        # Record the new fingerprint only AFTER the chunks were written
        # successfully. Saving the hash first and then crashing would mark the
        # page "already indexed" while its content was never stored — and it
        # would never be retried. Ordering side effects so a crash leaves you
        # retrying rather than silently skipping is a general rule.
        meta["hashes"][url] = page_hash
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        # RE-EMBED THE WHOLE SITE. Honest note on the trade-off: this is O(all
        # chunks) work for what was an O(one page) change, and on a large site
        # it is the slow part of this endpoint. Kept because it is simple and
        # obviously correct, and because the hash check above means it only runs
        # when a page genuinely changed. The optimisation — embedding just the
        # new chunks and splicing rows into the existing matrix — is a clear
        # future improvement, and one that Postgres makes natural in step 2.
        chunks, vecs = embed_site(sid)
        STATE["sites"][sid] = (chunks, vecs)
        invalidate_cache(sid)

        log.info("[ingest] %s %s: +%d, %d total",
                 sid, url, len(page_chunks), len(chunks))
        return Response({"site_id": sid, "chunks": len(chunks),
                         "status": "indexed",
                         "message": f"Indexed this page ({len(page_chunks)} "
                                    f"sections)."})
    finally:
        # Always, always release.
        lock.release()
