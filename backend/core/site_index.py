"""
===============================================================================
 site_index.py  —  the WHOLE-WEBSITE index the Chrome extension searches
===============================================================================

WHY THIS EXISTS
---------------
page_query.py only knows the page the visitor is looking at. To answer "where
do I apply?" when the answer lives on ANOTHER page, Compass needs to know the
rest of the website too. This module builds that knowledge in the background:

  1. The extension opens its side panel on a site. It reads the navigation of
     the live page — INCLUDING links hidden inside dropdown menus, which only
     exist after JavaScript runs — and sends us those links (POST /site).
  2. We crawl the site's PUBLIC pages from the server (no cookies, so never
     anything behind a login), starting with the root, then those menu links,
     then sitemap.xml, then whatever the pages link to. Max MAX_PAGES pages.
  3. Each page is chunked by heading (chunk_page below), embedded, and
     stored exactly like a Phase 1 widget site, in STATE["sites"].
  4. Menu links whose page gave us no text (a JavaScript-only page) still get a
     tiny "link chunk" (its menu label), so Compass can at least take people to
     that page.

Sites that are already indexed (openlake.in, crawled with Playwright) are used
as they are; only menu links that are missing from the index get crawled.

Everything heavy runs in a background thread, so POST /site returns at once
with {"state": "indexing"} and the extension polls until "ready".

SAFETY
------
* Only public hostnames are crawled: anything resolving to a private, loopback
  or link-local address is refused (otherwise anyone could make our server
  fetch internal URLs — "SSRF"). Redirects are followed by hand and re-checked.
* robots.txt is obeyed, at most MAX_CONCURRENT crawls run at once, and a site is
  re-crawled at most once every RECRAWL_AFTER seconds.
* Link text sent by the extension is only kept for URLs our own crawler could
  fetch, so a client can't invent pages.
"""

import ipaddress
import os
import re
import socket
import threading
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from core.indexer import UA, _is_html_url, _load_robots

MAX_PAGES = 80            # pages fetched per crawl
MAX_SEEDS = 80            # menu links accepted from the extension
MAX_SITEMAP_URLS = 150
MAX_CONCURRENT = 2        # crawls running at the same time on this server
RECRAWL_AFTER = 6 * 3600  # seconds before a site may be crawled again
TIMEOUT = 10
DELAY = 0.2
MAX_MAP_ENTRIES = 400     # rows of the site map sent back to the extension
CHUNK_CHARS = 1500        # text kept per section
VERSION = 3               # bump when chunk_page or the crawl changes, so old indexes get rebuilt

# Indexes made some other way that should never be replaced by a crawl
# (openlake.in was crawled with Playwright in Phase 1).
KEEP = set(filter(None, os.environ.get("COMPASS_KEEP_SITES", "openlake.in").split(",")))

# For local testing against http://localhost sites only. Never set on Render.
ALLOW_PRIVATE = os.environ.get("COMPASS_ALLOW_PRIVATE_HOSTS") == "1"

# sid -> {"state": "indexing" | "ready" | "failed", "pages": int, "at": float}
STATUS: dict = {}
_status_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


# ---------------------------------------------------------------------------
#  URLs
# ---------------------------------------------------------------------------

def clean_url(url: str) -> str:
    """scheme://host/path with no query, no #fragment and no trailing slash.
    The same form indexer.py stores, so URLs from both compare equal."""
    try:
        url = urldefrag(url)[0]
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        return f"{p.scheme}://{p.netloc.lower()}{p.path}".rstrip("/")
    except ValueError:
        return ""


def site_of(url: str) -> str:
    """'https://www.Example.com:443/x' -> 'example.com' (same rule as api.normalize_site_id)."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def on_site(url: str, sid: str) -> bool:
    """Same website: example.com and www.example.com count as one."""
    return site_of(url) == sid


def is_public_host(host: str) -> bool:
    """True if every address the host resolves to is on the public internet."""
    if ALLOW_PRIVATE:
        return True
    if not host or "." not in host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


def _get(session: requests.Session, url: str, sid: str):
    """GET with redirects followed by hand, so every hop is checked. Returns
    (final_url, response) or (None, None)."""
    for _ in range(4):
        if not (on_site(url, sid) and is_public_host(urlsplit(url).hostname or "")):
            return None, None
        try:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException:
            return None, None
        if r.is_redirect or r.is_permanent_redirect:
            url = urljoin(url, r.headers.get("location", ""))
            continue
        return url, r
    return None, None


# ---------------------------------------------------------------------------
#  The crawl
# ---------------------------------------------------------------------------

def _sitemap_urls(session, root: str, sid: str) -> list:
    """<loc> entries of /sitemap.xml (one level of sitemap index is followed)."""
    out, todo = [], [urljoin(root + "/", "/sitemap.xml")]
    for _ in range(4):
        if not todo or len(out) >= MAX_SITEMAP_URLS:
            break
        _, r = _get(session, todo.pop(0), sid)
        if r is None or r.status_code != 200:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        for loc in locs:
            if loc.endswith(".xml"):
                todo.append(loc)
            else:
                out.append(loc)
    return out[:MAX_SITEMAP_URLS]


def locale_prefix(page_url: str, links: list) -> str:
    """'/in' when the visitor is on a country version of the site (apple.com/in/…)
    and the site's own menus agree (most menu links start with /in/). Then we only
    crawl that country's pages, instead of mixing in other countries' pages (with
    other prices, dates and products) from the sitemap."""
    seg = urlsplit(page_url).path.strip("/").split("/")[0].lower()
    if not re.fullmatch(r"[a-z]{2}(-[a-z]{2,4})?", seg) or len(links) < 5:
        return ""
    paths = [urlsplit(l["url"]).path.lower() for l in links]
    share = sum(p == f"/{seg}" or p.startswith(f"/{seg}/") for p in paths) / len(paths)
    return f"/{seg}" if share >= 0.6 else ""


def crawl(root: str, sid: str, seeds: list, skip: set, max_pages: int = MAX_PAGES, prefix: str = ""):
    """Fetch public pages. Order: root, menu links (seeds), sitemap, then links
    found on the pages. Returns (pages, fetched) where pages is a list of
    {url, title, html}."""
    session = requests.Session()
    session.headers["User-Agent"] = UA
    rp = _load_robots(root)

    queue = deque()
    seen = set(skip)

    def push(u, front=False):
        u = clean_url(u)
        in_scope = not prefix or urlsplit(u).path.lower().rstrip("/") == prefix or urlsplit(u).path.lower().startswith(prefix + "/")
        if u and u not in seen and on_site(u, sid) and _is_html_url(u) and in_scope:
            seen.add(u)
            queue.appendleft(u) if front else queue.append(u)

    push(root)
    for s in seeds:
        push(s)
    for u in _sitemap_urls(session, root, sid):
        push(u)

    pages, fetched = [], {}   # fetched: requested URL -> URL it ended up at (redirects)
    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if rp and not rp.can_fetch(UA, url):
            continue
        final, r = _get(session, url, sid)
        ctype = r.headers.get("content-type", "") if r is not None else ""
        if r is None or r.status_code != 200 or "text/html" not in ctype:
            continue
        if "charset" not in ctype.lower():
            r.encoding = "utf-8"   # requests would guess Latin-1 and garble "·" or "₹"
        base, final = final, clean_url(final) or url
        fetched[url] = final
        fetched[final] = final
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""
        pages.append({"url": final, "title": title, "html": r.text})
        for a in soup.find_all("a", href=True):
            push(urljoin(base, a["href"]))
        time.sleep(DELAY)
    return pages, fetched


# ---------------------------------------------------------------------------
#  Turning a page into chunks
# ---------------------------------------------------------------------------

def _is_chrome(tag: Tag) -> bool:
    """Site navigation and footers, not content. A <header> only counts when it
    holds a menu (an article's <header> usually holds its title)."""
    if tag.name in ("nav", "footer", "aside"):
        return True
    if (tag.get("role") or "") in ("navigation", "contentinfo", "banner"):
        return True
    return tag.name == "header" and tag.find("nav") is not None


def chunk_page(html: str, url: str, title: str) -> list:
    """One chunk per h1-h3 holding ALL the text up to the next heading, plus one
    "page" chunk (title + meta description + text before the first heading).

    Walks the text in reading order, so it works however deeply a site nests its
    <div>s. (indexer.py's chunker only took the first piece of text of each
    element, which lost most of the text on sites like apple.com.)"""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "template", "svg", "form"]):
        t.decompose()
    for t in [t for t in soup.find_all(True) if _is_chrome(t)]:
        if not t.decomposed:
            t.decompose()
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    desc = (meta.get("content") or "").strip() if meta else ""

    sections, intro, current, inside = [], [], None, set()
    root = soup.body or soup
    for el in root.descendants:
        if isinstance(el, Tag) and el.name in ("h1", "h2", "h3"):
            heading = el.get_text(" ", strip=True)
            if heading:
                current = {"heading": heading[:200], "level": el.name, "parts": [], "n": 0}
                sections.append(current)
                inside = {id(d) for d in el.descendants}
            continue
        if not isinstance(el, NavigableString) or isinstance(el, Comment) or id(el) in inside:
            continue
        text = " ".join(str(el).split())
        if not text:
            continue
        bucket = current if current else None
        if bucket is None:
            if sum(len(x) for x in intro) < 1000:
                intro.append(text)
        elif bucket["n"] < CHUNK_CHARS:
            bucket["parts"].append(text)
            bucket["n"] += len(text) + 1

    base = {"url": url, "page_title": title, "selector": "", "v": VERSION}
    out = [{**base, "heading": title or url, "level": "page",
            "content": f"{title}. {desc} {' '.join(intro)}"[:1000].strip()}]
    for sec in sections:
        body = " ".join(sec["parts"])
        if sec["level"] == "h3" and len(body) < 5:
            continue  # a card title with nothing under it
        out.append({**base, "heading": sec["heading"], "level": sec["level"],
                    "content": f"{sec['heading']}. {body}"[:CHUNK_CHARS]})
    if len(sections) < 3:
        # Few headings (often a JavaScript page): windows of the page's text.
        text = " ".join(" ".join(s.split()) for s in root.stripped_strings)[:6000]
        for i in range(0, len(text), 1200):
            if len(text[i:i + 1200]) > 40:
                out.append({**base, "heading": title or url, "level": "p", "content": text[i:i + 1200]})
    return out


def link_chunk(link: dict, title: str = "") -> dict:
    """A one-line chunk for a page we can reach but couldn't read (JS-only)."""
    text = (link.get("text") or "").strip()[:120]
    menu = (link.get("menu") or "").strip()[:60]
    where = f"In the {menu} menu" if menu else "In the site navigation"
    return {
        "url": clean_url(link["url"]),
        "page_title": title or text,
        "heading": text,
        "level": "page",              # "go to the top of this page"
        "selector": "",
        "content": f"{text}. {where}: a link to the {text} page.",
        "v": VERSION,
    }


# ---------------------------------------------------------------------------
#  Building / topping up one site (runs in a background thread)
# ---------------------------------------------------------------------------

def _set(sid, **kw):
    with _status_lock:
        STATUS.setdefault(sid, {"state": "indexing", "pages": 0, "at": 0.0}).update(kw)


def build(sid: str, root: str, links: list, existing: list, save, page: str = ""):
    """Crawl `sid` and hand the merged chunk list to save(chunks).

    existing: the chunks already indexed for this site ([] for a new site).
    Only URLs that aren't in `existing` are fetched, unless the index is out of
    date (made by an older chunk_page), in which case everything is re-read.
    """
    if existing and outdated(sid, existing):
        existing = []
    if not _slots.acquire(blocking=False):
        # Other crawls are running: try again in a minute.
        _set(sid, state="ready" if existing else "failed", at=time.time(), retry=time.time() + 60)
        return
    try:
        have = {c["url"] for c in existing}
        seeds = [l["url"] for l in links]
        limit = MAX_PAGES if not existing else min(25, MAX_PAGES)
        prefix = locale_prefix(page, links)
        if prefix:
            root = root.rstrip("/") + prefix
        pages, fetched = crawl(root, sid, seeds, skip=have if existing else set(), max_pages=limit, prefix=prefix)
        if existing and not pages and not links:
            _set(sid, state="ready", at=time.time())
            return

        chunks = list(existing)
        for p in pages:
            chunks = [c for c in chunks if c["url"] != p["url"]]
            chunks.extend(chunk_page(p["html"], p["url"], p["title"]))

        # Menu links we fetched but got no text from: keep them as link chunks.
        covered = {c["url"] for c in chunks}
        titles = {p["url"]: p["title"] for p in pages}
        for l in links:
            u = clean_url(l["url"])
            if u and u in fetched and fetched[u] not in covered and l.get("text"):
                final = fetched[u]
                chunks.append(link_chunk({**l, "url": final}, titles.get(final, "")))
                covered.add(final)

        for i, c in enumerate(chunks):
            c["id"] = i
        if chunks:
            save(chunks)
        n_pages = len({c["url"] for c in chunks})
        _set(sid, state="ready" if chunks else "failed", pages=n_pages, at=time.time())
        print(f"[site] {sid}: {len(pages)} pages crawled, {len(chunks)} chunks, {n_pages} pages indexed")
    except Exception as e:  # never let a crawl crash the server
        print(f"[site] {sid} failed: {type(e).__name__}: {e}")
        _set(sid, state="ready" if existing else "failed", at=time.time())
    finally:
        _slots.release()


def needs_crawl(sid: str, chunks: list | None, links: list) -> bool:
    """Crawl if the site is new, or if it has menu links we haven't indexed —
    but never more often than RECRAWL_AFTER."""
    st = STATUS.get(sid)
    if st and st["state"] == "indexing":
        return False
    if st and time.time() < st.get("retry", 0):
        return False
    if not chunks or outdated(sid, chunks):
        return True
    have = {c["url"] for c in chunks}
    return any(clean_url(l["url"]) not in have for l in links)


def outdated(sid: str, chunks: list) -> bool:
    """Made by an older version of chunk_page (and not a site we keep as it is)."""
    return sid not in KEEP and any(c.get("v") != VERSION for c in chunks)


def start(sid, root, links, existing, save, page=""):
    _set(sid, state="indexing", retry=time.time() + RECRAWL_AFTER, pages=len({c["url"] for c in existing}), at=time.time())
    threading.Thread(target=build, args=(sid, root, links, existing, save, page), daemon=True).start()


def site_map(chunks: list) -> list:
    """What the extension needs for instant suggestions: one row per section."""
    rows, seen = [], set()
    for c in chunks:
        key = (c["url"], c["heading"])
        if key in seen or not c["heading"]:
            continue
        seen.add(key)
        rows.append({"url": c["url"], "title": c.get("page_title", ""),
                     "heading": c["heading"][:160], "level": c.get("level", "h2")})
        if len(rows) >= MAX_MAP_ENTRIES:
            break
    return rows
