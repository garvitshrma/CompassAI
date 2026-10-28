"""
===============================================================================
 crawler_js.py — crawler for JavaScript-rendered sites (the Playwright variant)
===============================================================================

Same output as crawler.py (pages/<netloc>/*.html + index.json) but runs a
real headless browser so client-side nav, dropdowns, and hydrated links
are visible. Use this for React/Next/Vue sites; use crawler.py for plain
server-rendered sites (it's faster).

-------------------------------------------------------------------------------
 WHY A WHOLE SECOND CRAWLER EXISTS
-------------------------------------------------------------------------------
`requests` downloads the HTML the server sends and stops there. For a classic
website that is the finished page. For a React / Next.js / Vue app it is not —
what arrives over the wire is roughly:

    <body>
      <div id="root"></div>
      <script src="/bundle.js"></script>
    </body>

Every word of visible content is created LATER, by JavaScript, in the browser.
`requests` cannot run JavaScript, so crawler.py sees an empty page and produces
zero chunks. This is the single most common reason a site "fails to index".

Playwright solves it by driving a real Chromium: it loads the page, executes the
bundle, waits for React to render ("hydration"), and only then do we read the
DOM. We get the same HTML a human would see.

    read more:
      Playwright Python .... https://playwright.dev/python/docs/intro
      Headless browsers .... https://developer.chrome.com/docs/chromium/headless
      Hydration ............ https://react.dev/reference/react-dom/client/hydrateRoot
      CSR vs SSR ........... https://web.dev/articles/rendering-on-the-web

-------------------------------------------------------------------------------
 WHY THIS CANNOT RUN ON THE SERVER
-------------------------------------------------------------------------------
A headless Chromium is roughly 300MB on disk and hundreds of MB of RAM in use.
Render's free tier gives the whole API 512MB. So this file runs on YOUR machine,
via register_site.py, and only the finished chunks.json is uploaded. The server
never launches a browser. That constraint is why the project has three separate
ingestion paths at all.

Usage:
    pip install playwright
    playwright install chromium     # downloads the actual browser binary, once
    python crawler_js.py https://openlake.in/
"""

import json
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# sync_playwright is the synchronous (blocking) API. Playwright also offers an
# async one, but for a sequential crawler the sync version is far easier to read
# and there is nothing to gain from concurrency here — we are rate-limiting
# ourselves on purpose anyway.
# read more: https://playwright.dev/python/docs/library
from playwright.sync_api import sync_playwright

MAX_DEPTH = 3
MAX_PAGES = 200

# HOW LONG TO WAIT AFTER LOAD, IN MILLISECONDS.
# This is the crude-but-reliable answer to "when is a React app finished?".
# There is no universal event that fires when hydration completes — different
# frameworks, different data-fetching libraries, different answers. A fixed
# generous wait is dumb but works everywhere.
# The precise alternative is page.wait_for_selector("main h1") or
# wait_for_load_state("networkidle"), which are faster but require knowing
# something about the specific site. For a general-purpose crawler that must
# handle sites we have never seen, dumb-and-universal wins.
WAIT_MS = 2500          # let the page hydrate + dropdowns render

# Per-page navigation timeout. Longer than crawler.py's 15s because we are
# waiting on a full browser: parse, network, JS execution, paint.
TIMEOUT_MS = 20000

UA = "CrawlerAI/0.1 (student project)"


# --- URL helpers: identical to crawler.py, see that file for full commentary --

def clean_url(url):
    """Drop the #fragment and trailing slash so /about, /about/ and /about#team
    all collapse to one canonical URL and are crawled once, not three times."""
    return urldefrag(url)[0].rstrip("/")


def same_domain(url, root_netloc):
    """Keep the crawl on the site we started from."""
    return urlparse(url).netloc == root_netloc


def is_html_url(url):
    """Skip obvious binaries by extension before spending a page load on them.
    This matters MORE here than in crawler.py: opening a PDF in a real browser
    is far more expensive than a wasted HTTP request."""
    path = urlparse(url).path.lower()
    bad = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
           ".zip", ".mp4", ".mp3", ".css", ".js", ".ico", ".woff", ".woff2")
    return not path.endswith(bad)


def slugify(url):
    """URL -> safe filename.  /blog/post-1 -> blog_post-1.html
    See crawler.py:slugify for the character-by-character explanation."""
    p = urlparse(url)
    slug = (p.path or "/").strip("/").replace("/", "_") or "index"
    if p.query:
        slug += "_" + re.sub(r"\W+", "-", p.query)
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)
    return slug[:120] + ".html"


def load_robots(root):
    """Respect robots.txt. Returning None means no rules were found, which the
    standard says should be read as 'crawling permitted'."""
    rp = RobotFileParser()
    rp.set_url(urljoin(root, "/robots.txt"))
    try:
        rp.read()
    except Exception:
        return None
    return rp


def crawl(root):
    """Breadth-first crawl, but every page is loaded in a real browser."""
    root = clean_url(root)
    root_netloc = urlparse(root).netloc
    rp = load_robots(root)

    # ---- output goes into a PER-DOMAIN subfolder --------------------------
    # crawler.py writes flat into pages/; this writes into pages/<domain>/.
    # That is better (you can crawl several sites without them overwriting each
    # other) but it is also an INCONSISTENCY you must know about: chunker.py
    # expects pages/index.json at the top level, which is exactly why the README
    # and register_site.py copy the per-domain files up one level afterwards.
    #
    # `.replace(":", "_")` handles a netloc like "localhost:3000" — a colon is
    # an illegal filename character on Windows.
    out_dir = Path("pages") / root_netloc.replace(":", "_")
    out_dir.mkdir(parents=True, exist_ok=True)

    seen = {root}
    queue = deque([(root, 0)])
    index = []

    # `with sync_playwright() as pw:` is a context manager that starts
    # Playwright's driver process and — crucially — guarantees it is shut down
    # even if the code inside raises. Without it, a crash could leave orphaned
    # Chromium processes eating memory on your machine.
    with sync_playwright() as pw:
        # headless=True means no visible window. Flip it to False when debugging
        # and you can literally watch the crawler click through the site, which
        # is by far the fastest way to understand why a page is not indexing.
        browser = pw.chromium.launch(headless=True)

        # ONE page (tab) reused for the entire crawl. Creating a fresh page per
        # URL would be slower and would leak memory over 200 iterations.
        page = browser.new_page(user_agent=UA)

        while queue and len(index) < MAX_PAGES:
            url, depth = queue.popleft()

            if rp and not rp.can_fetch(UA, url):
                print(f"[robots] skip {url}")
                continue

            try:
                # wait_until="domcontentloaded" resolves as soon as the HTML is
                # parsed, WITHOUT waiting for every image, font and analytics
                # script to finish. That is deliberate: the alternative "load"
                # can hang for many seconds on one slow third-party tracker, and
                # we do the real waiting ourselves via WAIT_MS below, which is
                # what we actually care about (JS execution, not images).
                # read more: https://playwright.dev/python/docs/api/class-page#page-goto
                page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
            except Exception as e:
                # str(e)[:80] truncates because Playwright's error messages are
                # enormous multi-line dumps that would drown the console.
                print(f"[error] {url} -> {str(e)[:80]}")
                continue

            # ==============================================================
            #  THE HOVER TRICK — the cleverest thing in this file
            # ==============================================================
            # PROBLEM: modern navigation menus do not put their links in the DOM
            # until you interact with them. A "Projects ▾" dropdown containing
            # links to six sub-pages renders those <a> tags only on hover or
            # click. So even a fully hydrated page can be HIDING most of the
            # site's structure, and a naive crawler silently misses whole
            # sections without ever erroring.
            #
            # SOLUTION: after the page settles, find everything that looks like
            # a menu trigger and hover each one. Whatever it reveals gets
            # rendered into the DOM, and since we read page.content() afterwards,
            # those links are captured and queued like any other.
            #
            # (check_links.py is the tool that surfaced this problem: it diffs
            #  "links seen" against "pages crawled", and the gap was all
            #  dropdown content.)
            try:
                page.wait_for_timeout(WAIT_MS)   # let hydration finish first

                # Three selectors, comma-separated, covering the common ways a
                # menu trigger is expressed:
                #   button              - the plain case
                #   [aria-haspopup]     - the accessibility attribute that
                #                         literally declares "I open a popup",
                #                         which is the most reliable signal
                #   nav [role='button'] - a div styled and labelled as a button
                # read more: https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Attributes/aria-haspopup
                buttons = page.query_selector_all("button, [aria-haspopup], nav [role='button']")

                for btn in buttons:
                    try:
                        # A short per-element timeout: an off-screen or covered
                        # element cannot be hovered and would otherwise stall.
                        btn.hover(timeout=1000)
                        page.wait_for_timeout(300)   # let the menu render
                    except Exception:
                        # NESTED try/except ON PURPOSE. Hovering is opportunistic
                        # — some of these "buttons" are cookie banners, modal
                        # closers or disabled controls, and any of them may throw.
                        # One failed hover must not abandon the other twenty, and
                        # must certainly not abandon the page.
                        pass
            except Exception:
                # And the outer guard means that if the hover phase fails
                # entirely, we still save the page as it stands. Degrading to
                # "crawler.py quality" beats losing the page.
                pass

            # page.content() serialises the CURRENT, LIVE DOM — after JS ran and
            # after our hovers. This is the whole reason the file exists: it is
            # emphatically NOT the same as the HTML `requests` would have
            # received from the server.
            html = page.content()

            # From here on it is identical to crawler.py: parse, save, extract
            # links, enqueue.
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(strip=True) if soup.title else ""

            fname = slugify(url)
            (out_dir / fname).write_text(html, encoding="utf-8")
            index.append({"url": url, "file": fname, "title": title, "depth": depth})
            print(f"[ok {len(index):3d}] d{depth} {title[:40]:40s} {url}")

            if depth < MAX_DEPTH:
                for a in soup.find_all("a", href=True):
                    nxt = clean_url(urljoin(url, a["href"]))
                    if (nxt not in seen
                            and same_domain(nxt, root_netloc)
                            and is_html_url(nxt)
                            and nxt.startswith("http")):
                        seen.add(nxt)
                        queue.append((nxt, depth + 1))

            # NOTE: there is no time.sleep() politeness delay here, unlike
            # crawler.py. It is not needed — a full browser page load plus
            # WAIT_MS (2.5s) plus the hover pass already means we hit the server
            # far more slowly than the 0.5s-delayed requests crawler does.

        browser.close()

    (out_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"\nDone. {len(index)} pages -> {out_dir}/  (see index.json)")
    # An honest reminder about the folder-layout inconsistency described above.
    print("Note: chunker.py reads pages/index.json — point it at this folder "
          "or copy the files up one level.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python crawler_js.py <root-url>")
    crawl(sys.argv[1])
