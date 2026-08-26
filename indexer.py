"""
===============================================================================
 indexer.py — crawl + chunk in ONE pass, inside the API process
===============================================================================

Runs inside the API process using requests + BeautifulSoup (no Playwright,
no subprocess). Designed to fit Render's 512MB. Won't handle JS-rendered
content, but works for the majority of simple/server-rendered sites.

-------------------------------------------------------------------------------
 WHY THIS FILE EXISTS WHEN crawler.py + chunker.py ALREADY DO THIS
-------------------------------------------------------------------------------
Different execution context, therefore different constraints:

    crawler.py + chunker.py     indexer.py
    ------------------------    -------------------------------------------
    run from YOUR terminal      runs inside the live API server
    write .html files to disk   holds everything in memory, no disk I/O
    two separate steps          one function call
    generous limits (200 pages) tight limits (100 pages), 512MB budget
    you can watch and retry     must never hang a visitor's HTTP request

The duplicated logic is a deliberate trade: keeping the two versions separate
means tuning the server's crawl behaviour cannot accidentally break the offline
pipeline, and the offline pipeline can stay generous without endangering
production. If you refactored them into one module you would need a config
object threading through everything, which is arguably worse.

THIS FILE ALSO OWNS THE SPA FALLBACK (chunk_rendered / _chunk_text_fallback at
the bottom), which the offline chunker does not have. That is the interesting
part — start there if you are skimming.

READ MORE
---------
  Single-page applications ... https://developer.mozilla.org/en-US/docs/Glossary/SPA
  Sliding-window chunking .... https://www.pinecone.io/learn/chunking-strategies/
  Server vs client rendering . https://web.dev/articles/rendering-on-the-web

Usage from api.py:
    from indexer import index_site
    chunks = index_site("https://example.com")
"""

import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
import hashlib
import requests
from bs4 import BeautifulSoup

# ---- SPA fallback tuning ----------------------------------------------------
# If heading-based chunking produces fewer than this many chunks, we suspect the
# page has no real heading structure (very common on React/Tailwind landing
# pages, which style <div>s to look like headings) and try text windows instead.
# Three is a reasonable "the author clearly did use headings" threshold.
MIN_HEADING_CHUNKS = 3       # below this, fall back to text windows

# Size of each sliding text window, in characters. Roughly 200-300 words, which
# is about one topic's worth of prose and sits comfortably inside the embedding
# model's token limit.
FB_WINDOW = 1200

# How much each window repeats from the previous one. THIS IS THE IMPORTANT ONE:
# without overlap, a sentence that happens to straddle a boundary is split in
# half and neither half carries its full meaning — so a query about it matches
# neither chunk. Overlap guarantees every span of text appears intact in at
# least one window. The cost is ~12% redundant storage, which is cheap insurance.
FB_OVERLAP = 150

# ---- crawl limits, all TIGHTER than crawler.py's -----------------------------
# Every one of these is smaller because this code runs on a shared 512MB box
# while a visitor waits on the other end of an HTTP request.
MAX_DEPTH = 3
MAX_PAGES = 100      # crawler.py allows 200; halved to bound memory and time
DELAY = 0.3          # crawler.py uses 0.5; shortened so a visitor waits less
TIMEOUT = 12         # crawler.py uses 15; fail faster on a slow server
UA = "CompassAI/0.1 (auto-indexer)"

HEADINGS = ["h1", "h2", "h3"]
CHROME_TAGS = ["nav", "header", "footer", "aside"]
MIN_CHARS = 20
MAX_CHARS = 1500


# ---------- crawler ----------
# These mirror crawler.py. See that file for the full explanations of BFS,
# robots.txt, fragment stripping and the extension filter — the comments here
# cover only what DIFFERS.

def _clean_url(url):
    """Strip #fragment and trailing slash so URL variants dedupe correctly."""
    return urldefrag(url)[0].rstrip("/")


def _is_excluded(url, patterns):
    """True if this URL's path matches any site-owner exclusion pattern.

    WHY THIS IS DATA AND NOT AN `if` IN THE CRAWLER
    -----------------------------------------------
    Sitemap seeding correctly finds pages that no link points to. Sometimes the
    site owner does not want those surfaced anyway — a legal imprint, an
    internal IT page — even though they are real, public, and listed. That is a
    judgement about the SITE, not about crawling, so it belongs beside the
    site's data (sites/<id>/exclude.json), not in shared code. Hardcoding one
    customer's paths here would silently apply them to every other customer.

    Patterns are matched against the path only, so they stay stable across
    http/https and www/non-www:

        "/imprint"        exact path
        "/internal/*"     that path and everything beneath it

    A missing or empty exclude.json means "index everything", which keeps the
    zero-config onboarding story intact — this only ever costs you something if
    you deliberately opt in.
    """
    if not patterns:
        return False
    path = urlparse(url).path.rstrip("/").lower() or "/"
    for pat in patterns:
        p = str(pat).strip().rstrip("/").lower() or "/"
        if p.endswith("/*"):
            base = p[:-2]
            if path == base or path.startswith(base + "/"):
                return True
        elif path == p:
            return True
    return False


# Public alias. /ingest in api.py must apply the SAME rule as the crawler:
# excluding a page from crawling but still letting a visitor's browser post it
# back through /ingest would quietly reintroduce it, and the exclusion would
# appear to "stop working" some time after every deploy.
is_excluded = _is_excluded


def _same_domain(url, root_netloc):
    """Stay on the site we started from; never wander onto external links."""
    return urlparse(url).netloc == root_netloc


def _is_html_url(url):
    """Free extension-based pre-filter so we do not spend a request on a PDF."""
    path = urlparse(url).path.lower()
    bad = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
           ".zip", ".mp4", ".mp3", ".css", ".js", ".ico", ".woff", ".woff2")
    return not path.endswith(bad)


def _sitemap_urls(root, session):
    """Return every URL listed in the site's sitemap(s), or [] if there is none.

    WHY THIS IS THE HIGHEST-VALUE SEED WE HAVE
    ------------------------------------------
    A BFS crawl can only reach pages that some OTHER page links to in its
    SERVER-RENDERED html. On a modern JS site the navigation is built by the
    framework at runtime, so `requests` sees a header with almost no <a> tags
    and most of the site is unreachable — not missing, not blocked, just
    invisible.

    That is not hypothetical. Measured on openlake.in with a real browser,
    opening every menu and disclosure on every page, desktop and mobile:

        reachable by a non-JS crawler   8  (/, 4 nav pages, /blog, 2 posts)
        reachable by a human clicking  12  (+ 4 /resources/* behind a desktop
                                            dropdown, invisible on mobile)
        listed in sitemap.xml          16  (+ /past-community, /it-admins,
                                            /imprint, /safeguarding)

    Those last four are TRUE ORPHANS — no page links to them at all. Only the
    sitemap knows they exist, and /past-community is exactly the kind of page a
    visitor wants and cannot find, which is the case Compass exists to solve.

    So: seed the frontier from the sitemap, then let BFS run as well. The two
    sources are complementary and neither is complete on its own — the sitemap
    misses pages that exist but were never listed (/newsletterpage here), and
    the link crawl misses everything above.

    Whether an orphan SHOULD be surfaced is the site owner's call, not the
    crawler's — see _is_excluded.

    We follow the sitemap protocol far enough to be useful and no further:
    <sitemapindex> (a sitemap of sitemaps) is expanded one level, which covers
    essentially every real site.
    read more: https://www.sitemaps.org/protocol.html
    """
    candidates = []

    # robots.txt may name the sitemap explicitly, and that is more reliable than
    # guessing — some sites put it at a non-standard path.
    try:
        r = session.get(urljoin(root, "/robots.txt"), timeout=TIMEOUT)
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    candidates.append(line.split(":", 1)[1].strip())
    except Exception:
        pass

    # The conventional location, tried whether or not robots.txt mentioned one.
    # dict.fromkeys dedupes while preserving order.
    candidates.append(urljoin(root, "/sitemap.xml"))
    candidates = list(dict.fromkeys(candidates))

    found = []
    seen_maps = set()
    while candidates and len(found) < MAX_PAGES:
        sm = candidates.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            r = session.get(sm, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            # A 404 handler that returns HTML with status 200 is common; parsing
            # it as XML just yields no <loc> tags, which is harmless.
            locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, re.I | re.S)
        except Exception:
            continue

        # <sitemapindex> nests sitemaps inside <loc> too. Distinguish by the
        # root element rather than by the URL, which can be named anything.
        if "<sitemapindex" in r.text[:2000].lower():
            candidates.extend(locs[:20])          # bound the fan-out
            continue
        found.extend(locs)

    return found


def _load_robots(root):
    """Read robots.txt; None means 'no restrictions stated, crawl freely'.

    Still honoured even in the auto-indexing path. That is not just etiquette:
    this endpoint is unauthenticated, so any visitor can point us at any domain,
    and obeying robots.txt is what keeps Compass a well-behaved indexer rather
    than an open scraping proxy.
    """
    rp = RobotFileParser()
    rp.set_url(urljoin(root, "/robots.txt"))
    try:
        rp.read()
    except Exception:
        return None
    return rp


def _crawl(root, exclude=None):
    """Fetch pages breadth-first, return list of {url, title, html}.

    `exclude` is the site's list of path patterns to skip (see _is_excluded).

    KEY DIFFERENCE FROM crawler.py: nothing is written to disk. Pages accumulate
    in the `pages` list in RAM and are handed straight to the chunker. That is
    the right call inside a web server — the container's filesystem is ephemeral
    anyway, and skipping disk I/O keeps the request fast.

    THE MEMORY IMPLICATION, stated honestly: 100 pages x ~200KB of HTML is
    roughly 20MB held at once. Comfortable inside 512MB, but it IS the reason
    MAX_PAGES is 100 here and not 200.
    """
    root = _clean_url(root)
    root_netloc = urlparse(root).netloc
    rp = _load_robots(root)

    session = requests.Session()          # connection reuse; see crawler.py
    session.headers["User-Agent"] = UA

    seen = {root}                         # O(1) dedupe of queued URLs
    queue = deque([(root, 0)])            # BFS frontier of (url, depth)
    pages = []

    # ---- SEED THE FRONTIER FROM THE SITEMAP -------------------------------
    # Everything here goes in at depth 0, so sitemap pages are fetched before
    # anything BFS discovers and are never cut off by MAX_DEPTH. Pages listed
    # in the sitemap are the ones the site owner considers real; links found by
    # crawling are a best-effort supplement. See _sitemap_urls for why a link
    # crawl alone cannot see most of a JS-rendered site.
    for u in _sitemap_urls(root, session):
        u = _clean_url(u)
        if (u not in seen and _same_domain(u, root_netloc)
                and _is_html_url(u) and u.startswith("http")
                and not _is_excluded(u, exclude)):
            seen.add(u)
            queue.append((u, 0))
    if len(seen) > 1:
        print(f"[crawl] sitemap seeded {len(seen) - 1} url(s)")

    while queue and len(pages) < MAX_PAGES:
        url, depth = queue.popleft()      # popleft => breadth-first

        if rp and not rp.can_fetch(UA, url):
            continue

        try:
            r = session.get(url, timeout=TIMEOUT)
        except requests.exceptions.SSLError:
            # ---- SSL FALLBACK (not present in crawler.py) -----------------
            # Small sites, university departments and internal tools very often
            # have an expired, self-signed or misconfigured TLS certificate.
            # Refusing to index them would mean a whole category of real
            # customers simply cannot use the product.
            #
            # verify=False disables certificate validation, which means we lose
            # protection against a man-in-the-middle for this fetch. The risk is
            # acceptable HERE because we are only reading public marketing
            # copy — no credentials, no personal data, nothing is sent. You
            # would NEVER do this on a request carrying a secret.
            # read more: https://requests.readthedocs.io/en/latest/user/advanced/#ssl-cert-verification
            try:
                r = session.get(url, timeout=TIMEOUT, verify=False)
            except Exception:
                continue
        except Exception:
            # Note: no print() here, unlike crawler.py. In a server, logging
            # every dead link on every crawl floods the log with noise.
            continue

        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "text/html" not in ctype:
            continue

        # ---- RECORD WHERE WE LANDED, NOT WHERE WE AIMED --------------------
        # requests follows redirects by default, so `r.url` may differ from the
        # `url` we requested. Storing the REQUESTED url here was a real and
        # actively harmful bug: openlake.in 301s /fiscal-sponsorship ->
        # /resources/web-development, so the index ended up holding web-dev
        # resource content under a url whose page is titled "Fiscal
        # Sponsorship". Compass's entire promise is "we take you to the exact
        # place"; sending a visitor to the wrong URL is the worst thing this
        # system can do, and it fails silently because the redirect resolves.
        #
        # It also breaks two other things: the widget's samePage check compares
        # data.url against location.href and would never match, and re-crawling
        # via the sitemap (which lists the POST-redirect url) would index the
        # same page a SECOND time under its real name.
        final = _clean_url(r.url)
        # RE-CHECK AFTER THE REDIRECT. An allowed url can land on an excluded
        # one; checking only the requested url would let the destination in
        # through the back door.
        if _is_excluded(final, exclude):
            time.sleep(DELAY)
            continue
        if final != url:
            # Mark the alias seen too, so a later link to the pre-redirect url
            # does not queue a duplicate fetch of the same page.
            seen.add(final)

        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""

        # Two different urls can redirect to the same destination; without this
        # the page would be chunked twice and both copies embedded.
        if any(p["url"] == final for p in pages):
            time.sleep(DELAY)
            continue

        # Store the raw HTML in memory instead of writing a file.
        pages.append({"url": final, "title": title, "html": r.text})

        if depth < MAX_DEPTH:
            for a in soup.find_all("a", href=True):
                # urljoin against `final`, not `url`: a relative href on a page
                # reached through a redirect resolves against the page's real
                # location. /conduct -> /resources/app-development means href
                # "./web-development" is /resources/web-development, not
                # /web-development.
                nxt = _clean_url(urljoin(final, a["href"]))
                if (nxt not in seen
                        and _same_domain(nxt, root_netloc)
                        and _is_html_url(nxt)
                        and nxt.startswith("http")       # rejects mailto:/tel:
                        and not _is_excluded(nxt, exclude)):
                    seen.add(nxt)
                    queue.append((nxt, depth + 1))

        time.sleep(DELAY)

    return pages


# ---------- chunker ----------
# Functionally identical to chunker.py. Read that file for the full reasoning
# behind in_chrome / css_path / text_until_next_heading — those are the deepest
# explanations in the codebase and they are not repeated here.

def _in_chrome(tag):
    """True if this heading sits inside nav/header/footer chrome.
    Without this filter, repeated navigation text swamps the index and every
    query matches the navbar. See chunker.py:in_chrome for the full story."""
    for parent in tag.parents:
        if parent.name in CHROME_TAGS:
            return True
        classes = " ".join(parent.get("class", [])).lower()
        if any(w in classes for w in ("navbar", "nav-", "footer", "menu")):
            return True
    return False


def _css_path(tag):
    """Generate the CSS selector the browser widget will scroll to.

    THE PRODUCT'S DIFFERENTIATOR, in one function. Prefers "#id" when available;
    otherwise walks up recording ":nth-of-type(n)" positions, stopping early at
    the first id'd ancestor. Full annotated version in chunker.py:css_path.
    read more: https://developer.mozilla.org/en-US/docs/Web/CSS/:nth-of-type
    """
    if tag.get("id"):
        return f"#{tag['id']}"
    parts = []
    node = tag
    while node is not None and node.name != "[document]":
        parent = node.parent
        if parent is None or parent.name == "[document]":
            parts.append(node.name)
            break
        # recursive=False is essential: CSS's :nth-of-type counts only DIRECT
        # siblings, so searching the whole subtree would give wrong indices.
        siblings = [s for s in parent.find_all(node.name, recursive=False)]
        if len(siblings) > 1:
            idx = siblings.index(node) + 1      # +1: CSS counts from 1
            parts.append(f"{node.name}:nth-of-type({idx})")
        else:
            parts.append(node.name)
        if parent.get("id"):
            parts.append(f"#{parent['id']}")    # anchor here, stop climbing
            break
        node = parent
    return " > ".join(reversed(parts))          # built bottom-up, CSS reads top-down


def _level(tag):
    """'h2' -> 2. Lets us compare heading RANK, not just heading identity."""
    return int(tag.name[1])


def _text_until_next_heading(heading):
    """Collect body text from this heading until the next same-or-higher rank one.

    The `own` set skips the heading's own child elements so its text is not
    duplicated into the body; `recursive=False` on find() takes only each
    element's direct text so nested tags are not harvested twice. See
    chunker.py:text_until_next_heading for the detailed walkthrough.
    """
    out = []
    my_level = _level(heading)
    own = set(id(d) for d in heading.descendants)
    for el in heading.find_all_next():
        if id(el) in own:
            continue
        if el.name in HEADINGS and _level(el) <= my_level:
            break                                   # next section starts here
        if el.name in ("p", "li", "td", "span", "a", "div"):
            t = el.find(string=True, recursive=False)
            if t and t.strip():
                out.append(t.strip())
        if sum(len(x) for x in out) > MAX_CHARS:
            break
    return " ".join(out)[:MAX_CHARS]


def _chunk_page(html, url, title):
    """One page of HTML -> a list of heading-scoped chunk dicts."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove script/style/noscript before extracting text, or minified
    # JavaScript ends up inside your embeddings and poisons search.
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    chunks = []
    for h in soup.find_all(HEADINGS):
        if _in_chrome(h):
            continue
        heading_text = h.get_text(" ", strip=True)
        if not heading_text:
            continue
        body = _text_until_next_heading(h)
        content = f"{heading_text}. {body}".strip()

        # An empty h1/h2 is still a valid navigation destination ("Gallery" over
        # a grid of images); an empty h3 is a meaningless sub-label. Note this
        # single-line form is equivalent to chunker.py's nested version.
        if len(content) < MIN_CHARS and h.name == "h3":
            continue

        chunks.append({
            "url": url,
            "page_title": title,
            "heading": heading_text,
            "level": h.name,
            "selector": _css_path(h),      # <- where the widget will scroll
            "content": content,
        })
    return chunks


# ---------- public API ----------
# Functions WITHOUT a leading underscore are the module's intended interface.
# The underscore-prefixed ones above are internal implementation detail.

def index_site(root_url, exclude=None):
    """
    Crawl a site and return a list of chunks ready for embedding.
    Lightweight: requests + BS4 only, no Playwright, no disk I/O.
    Returns [] if the site can't be reached or has no content.

    This is what api.py's /auto-register calls. The empty-list return is the
    contract that lets the caller try https:// and then http:// and then report
    a friendly failure — a function that raised instead would make that flow
    much uglier.
    """
    # Convenience: callers store bare hostnames ("example.com"), so add a scheme
    # if it is missing rather than failing on a URL that is obviously intended.
    if not root_url.startswith("http"):
        root_url = "https://" + root_url

    pages = _crawl(root_url, exclude)
    if not pages:
        return []                          # unreachable, or robots-blocked

    all_chunks = []
    for page in pages:
        cs = _chunk_page(page["html"], page["url"], page["title"])
        all_chunks.extend(cs)

    # Sequential ids that must equal the row index in embeddings.npy. See the
    # long note in chunker.py:main — this invariant is what makes search a
    # single matrix multiply with no lookup table.
    for i, c in enumerate(all_chunks):
        c["id"] = i

    return all_chunks


def chunk_html(html, url, title):
    """Public wrapper: heading-based chunking of one rendered page.

    A thin passthrough to the private _chunk_page. Exposing a public name means
    callers outside this module are not reaching into a `_private` function,
    so the internals stay free to change.
    """
    return _chunk_page(html, url, title)


def _visible_text(html):
    """Flatten a whole page into one clean string of human-visible text.

    Used only by the SPA fallback below, where we have given up on structure and
    just want the words.

    TWO STAGES OF CLEANUP:
      1. decompose() removes script/style/noscript AND all chrome tags. Note the
         list concatenation `[...] + CHROME_TAGS` — unlike _chunk_page we strip
         nav/header/footer wholesale here, because with no headings to anchor to
         we have no other way to tell chrome from content.
      2. The regex collapses runs of whitespace. HTML source is full of newlines
         and indentation that get_text preserves; `\\s+` matches any run of
         whitespace (spaces, tabs, newlines) and we replace each run with a
         single space. Without this, chunk boundaries and character counts would
         be dominated by invisible formatting.
    read more: https://docs.python.org/3/library/re.html#re.sub
    """
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"] + CHROME_TAGS):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _chunk_text_fallback(text, url, title):
    """Overlapping text windows for heading-less SPA pages.

    ===========================================================================
     THE SLIDING WINDOW, THE STANDARD FALLBACK CHUNKING STRATEGY
    ===========================================================================

    When a page has no usable headings, we cannot split on structure, so we
    split on LENGTH: take 1200 characters, then step forward by
    (1200 - 150) = 1050 and take another 1200, and so on.

        |------- window 1 -------|
                          |------- window 2 -------|
                          ^^^^^^^^
                          150 chars of overlap

    WHY OVERLAP: a sentence sitting exactly on a boundary would otherwise be
    cut in half, and neither half would carry the full meaning, so a query
    about it matches nothing well. Overlap guarantees every span appears whole
    somewhere.

    THE IMPORTANT LIMITATION — note "selector": "" below. These chunks have NO
    element to anchor to, so the widget cannot scroll to a precise spot; it can
    only send the visitor to the page. The product degrades gracefully from
    "scroll to this exact heading" down to "here is the right page" rather than
    failing outright. Being able to explain this trade-off is worth more than
    pretending the system always works perfectly.
    """
    if len(text) < MIN_CHARS:
        return []

    # Multiple assignment: out=[], start=0, n=len(text) on one line.
    out, start, n = [], 0, len(text)

    while start < n:
        # min() clamps the end to the text length so the final (short) window
        # does not run past the end.
        end = min(start + FB_WINDOW, n)
        window = text[start:end]

        # ---- try to end on a sentence boundary ---------------------------
        # `if end < n` means "this is not the last window" — no point trimming
        # the final one, its end is the end of the text.
        if end < n:
            # rfind searches BACKWARDS for the last ". " in the window, giving
            # us the latest sentence end that still fits.
            cut = window.rfind(". ")

            # THE GUARD IS THE SUBTLE PART: only accept the cut if it lands past
            # the halfway mark. Otherwise a page whose only period appears in
            # the first 50 characters would produce a 50-character chunk, and
            # then the next window starts there — so we would grind through the
            # page in tiny useless pieces. Better a slightly ragged 1200-char
            # window than a stream of fragments.
            if cut > FB_WINDOW // 2:          # // is integer division
                window = window[:cut + 1]     # +1 keeps the "." itself
                # Recompute `end` from the ACTUAL length kept, so the next
                # window's start position stays correct.
                end = start + len(window)

        out.append({
            "url": url, "page_title": title,
            # No heading exists, so fall back to the page title (or the URL) as
            # the human-readable label the widget will display.
            "heading": title or url,
            # "p" rather than "h1"/"h2"/"h3". This flows into retrieval.py's
            # LEVEL_BONUS lookup, which uses .get(level, 0.0) — so these chunks
            # correctly receive a neutral 0.0 bonus instead of crashing.
            "level": "p",
            "selector": "",                       # no element anchor → page-level
            "content": window.strip(),
        })

        # Termination check BEFORE stepping. If we just emitted the final
        # window, stop — otherwise the overlap subtraction below would move
        # `start` backwards from the end and we would loop forever.
        if end >= n:
            break

        # THE SLIDE: step forward, minus the overlap. This is the one line that
        # makes the windows overlap rather than merely abut.
        start = end - FB_OVERLAP
    return out


def chunk_rendered(html, url, title):
    """Widget-supplied rendered HTML: prefer headings, fall back to text.

    THE STRATEGY-SELECTION FUNCTION. Called by /ingest in api.py, and this is
    where the system decides how to handle a page it knows nothing about.

    THE DECISION, in three steps:

      1. Try structure first. Heading chunks are strictly better — they carry a
         real heading for display AND a CSS selector for precise scrolling.

      2. If there are at least MIN_HEADING_CHUNKS (3), trust them and return
         immediately. The site author gave us an outline; use it.

      3. Otherwise the page is probably a heading-less SPA landing page.
         Generate text windows too, then RETURN WHICHEVER PRODUCED MORE.

    Step 3's final comparison is the nice touch. It is not "always prefer the
    fallback" — if a page yields 2 good heading chunks and the flattened text is
    so short it yields only 1 window, the headings still win (`>=` favours them
    on a tie, since they are the better kind of chunk). We only give up
    structure when the fallback genuinely recovers more content.
    """
    heading_chunks = _chunk_page(html, url, title)
    if len(heading_chunks) >= MIN_HEADING_CHUNKS:
        return heading_chunks
    fb = _chunk_text_fallback(_visible_text(html), url, title)
    return heading_chunks if len(heading_chunks) >= len(fb) else fb
