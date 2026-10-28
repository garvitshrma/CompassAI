"""
===============================================================================
 navigator/services.py — domain helpers with no HTTP in them.
===============================================================================

These four functions were defined inside api.py but have nothing to do with the
web framework. Pulling them out here means views.py contains only request and
response handling, and these can be imported and tested on their own.

Nothing in this file changed during the port except `print()` becoming
`log.info()`, because gunicorn may buffer stdout where uvicorn did not.
"""

import json
import logging

from core.retrieval import embed_site, site_dir

from navigator.state import STATE, get_site_lock, invalidate_cache

log = logging.getLogger("compass")


def normalize_site_id(raw: str) -> str:
    """
    Reduce anything URL-shaped to one canonical site key.

        "https://www.Example.com:8080/pricing?x=1"  ->  "example.com"
        "WWW.EXAMPLE.COM"                           ->  "example.com"
        "example.com"                               ->  "example.com"

    WHY THIS FUNCTION IS LOAD-BEARING
    ---------------------------------
    The widget sends `location.hostname` from whatever page the visitor is on.
    If we did not normalise, a site registered as "example.com" would appear
    UNREGISTERED to a visitor who arrived via "www.example.com" — the classic
    multi-tenant lookup bug.

    Every entry point funnels through this one function, so the mapping is
    consistent. One canonicalisation function used everywhere beats five
    slightly-different inline cleanups.
    """
    # `(raw or "")` guards against None — None.strip() would raise.
    s = (raw or "").strip().lower()

    # Strip the scheme. split("//") on "https://x.com" gives ["https:", "x.com"]
    # and [-1] takes the last piece. With no "//" the split returns a
    # one-element list and [-1] is the original string, so this is safe either
    # way with no if-statement.
    s = s.split("//")[-1]

    s = s.split("/")[0]      # drop any path:  "x.com/a/b" -> "x.com"
    s = s.split(":")[0]      # drop any port:  "x.com:8080" -> "x.com"

    # Treat "www.example.com" and "example.com" as the same customer.
    if s.startswith("www."):
        s = s[4:]
    return s


def origin_ok(origin: str, sid: str) -> bool:
    """
    Anti-poisoning guard for /ingest.

    THE ATTACK THIS PREVENTS
    ------------------------
    /ingest writes into a site's index. Without a check, anyone could POST
    {"site_id": "some-bank.com", "html": "<h1>Send money here</h1>"} and poison
    a real customer's index — visitors to that bank would then be navigated to
    attacker-authored text. This is "index poisoning".

    WHY THE `Origin` HEADER IS A MEANINGFUL SIGNAL
    ----------------------------------------------
    On any cross-origin POST the BROWSER itself stamps Origin with the page's
    real origin, and JavaScript on the page CANNOT change it — it is on the
    forbidden-header list precisely so it can be trusted.

    ITS HONEST LIMIT: `curl -H "Origin: ..."` can send anything. This stops
    browser-based and casual poisoning; it is not an authentication boundary.
    The real fix is a per-site key issued at signup and embedded in the script
    tag — a natural thing to add once the dashboard exists.
    """
    # No Origin at all: server-to-server, curl, or our own tooling. Allowed,
    # because anyone able to omit the header could equally forge it, so blocking
    # adds friction without adding security.
    if not origin:
        return True

    # Same canonicalisation as everywhere else, so we compare like with like.
    o = normalize_site_id(origin)

    # Development allowances. "null" is what browsers send for a page opened
    # from disk via file://.
    if o in ("localhost", "127.0.0.1", "null", ""):
        return True

    # Exact match, or a subdomain. The "." in `"." + sid` is essential: without
    # it, endswith("example.com") would also accept the attacker-owned domain
    # "notexample.com" — a one-character bug that would defeat the whole check.
    return o == sid or o.endswith("." + sid)


def read_site_json(sid, name, default):
    """Read a JSON file from a site's folder, returning `default` on any problem.

    WHY SWALLOW THE EXCEPTION?
    Both files this reads are caches we can rebuild. If meta.json is corrupt
    (the process was killed mid-write) the correct behaviour is to treat it as
    empty and rebuild — not to 500 and make the site permanently unusable. Note
    we are lenient about recoverable state but NOT about the request itself,
    which the serializers validate strictly.
    """
    p = site_dir(sid) / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass          # fall through to the default
    return default


def save_site_callback(sid):
    """Build the callback that site_index.build() invokes when a crawl finishes.

    A function that returns a function, so the site id is captured in a closure
    and site_index does not need to know anything about our state layout.
    """
    def save(chunks):
        with get_site_lock(sid):
            d = site_dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "chunks.json").write_text(
                json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            STATE["sites"][sid] = embed_site(sid)
            invalidate_cache(sid)
            log.info("[site] %s crawl saved: %d sections", sid, len(chunks))
    return save
