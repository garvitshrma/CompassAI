"""
===============================================================================
 crawler.py  —  STAGE 1 of the Compass pipeline (static / server-rendered sites)
===============================================================================

WHAT THIS FILE DOES
-------------------
Given one starting URL, it walks the whole website by following <a href> links,
downloads the raw HTML of every page it finds, and saves each page to disk as a
.html file. It also writes an index.json that remembers which file came from
which URL.

WHY WE SAVE TO DISK INSTEAD OF PROCESSING IMMEDIATELY
-----------------------------------------------------
Crawling is slow (network) and chunking is fast (CPU). If we combined them and
the chunker had a bug, we would have to re-download the entire site just to try
again. By dumping raw HTML to disk once, we can re-run chunker.py a hundred
times for free. This is a general principle: separate the expensive irreversible
step from the cheap repeatable step.

WHERE THIS SITS IN THE PIPELINE
-------------------------------
    crawler.py  ->  pages/*.html + pages/index.json
    chunker.py  ->  chunks.json
    search.py / retrieval.py -> embeddings.npy
    answer.py   ->  the grounded LLM reply
    api.py      ->  serves it over HTTP
    compass-widget.js -> scrolls + highlights in the browser

WHEN TO USE THIS FILE VS crawler_js.py
--------------------------------------
Use THIS file when the site is "server-rendered", meaning the HTML that arrives
over the network already contains the visible text. Classic HTML sites,
WordPress, Django/Flask templates, Jekyll/Hugo blogs, etc.

Use crawler_js.py when the site is a React/Next/Vue "single page app" (SPA).
Those ship an almost-empty HTML shell and build the page with JavaScript AFTER
it arrives, so `requests` would see nothing but <div id="root"></div>.

READ MORE
---------
  Web crawling concepts .... https://en.wikipedia.org/wiki/Web_crawler
  requests library ......... https://requests.readthedocs.io/en/latest/
  BeautifulSoup docs ....... https://www.crummy.com/software/BeautifulSoup/bs4/doc/
  robots.txt spec .......... https://developers.google.com/search/docs/crawling-indexing/robots/intro
  Breadth-first search ..... https://en.wikipedia.org/wiki/Breadth-first_search

Usage:
    python crawler.py https://openlake.iitbhilai.ac.in

Output:
    pages/            one .html file per page
    pages/index.json  url -> filename, title, depth
"""

# --- standard library imports -------------------------------------------------
import json      # to write index.json (a list of dicts) out as text
import re        # regular expressions, used to sanitise filenames
import sys       # to read command-line arguments (sys.argv) and exit (sys.exit)
import time      # for time.sleep(), our politeness delay between requests

# `deque` = "double ended queue". A normal Python list is slow when you remove
# from the FRONT (it has to shift every other element left, an O(n) operation).
# A deque removes from the front in O(1). Since a BFS crawler pops from the
# front on every single page, this is the right data structure.
# read more: https://docs.python.org/3/library/collections.html#collections.deque
from collections import deque

# `Path` is the modern object-oriented way to handle file paths. It beats
# string concatenation because `Path("pages") / "a.html"` automatically uses the
# correct separator on Windows (\) vs Linux (/). Compass is developed on Windows
# and deployed on Linux, so this matters.
# read more: https://docs.python.org/3/library/pathlib.html
from pathlib import Path

# URL manipulation helpers from the standard library:
#   urljoin  - turns a relative link ("/about") plus the current page into an
#              absolute URL ("https://site.com/about"). Handles ../ too.
#   urlparse - splits a URL into its parts (scheme, netloc, path, query, ...)
#   urldefrag- splits off the "#section" fragment at the end of a URL
# read more: https://docs.python.org/3/library/urllib.parse.html
from urllib.parse import urljoin, urlparse, urldefrag

# RobotFileParser reads a site's /robots.txt and can answer "am I allowed to
# fetch this URL?". Respecting robots.txt is the basic etiquette of crawling —
# ignoring it can get your IP banned and, for a commercial product, is a legal
# and reputational risk.
# read more: https://docs.python.org/3/library/urllib.robotparser.html
from urllib.robotparser import RobotFileParser

# --- third-party imports ------------------------------------------------------
import requests                    # the HTTP client everyone uses
from bs4 import BeautifulSoup      # the HTML parser everyone uses


# =============================================================================
#  TUNING CONSTANTS
#  These are the "knobs" of the crawler. They are deliberately at the top of the
#  file, in CAPITALS, so you can change behaviour without hunting through code.
#  Naming constants in CAPS is a Python convention (see PEP 8).
#  read more: https://peps.python.org/pep-0008/#constants
# =============================================================================

# How many link-hops away from the starting page we are willing to travel.
# depth 0 = the homepage itself
# depth 1 = every page linked from the homepage
# depth 2 = every page linked from THOSE pages
# depth 3 = one more level
# Why cap it at all? Because a site with a calendar or a paginated archive can
# generate effectively INFINITE URLs, and without a cap the crawler never stops.
MAX_DEPTH = 3

# Hard ceiling on total pages. A second, independent safety net: even if the
# depth limit is generous, we will never download more than 200 pages. Two
# independent limits is a deliberate belt-and-braces choice.
MAX_PAGES = 200

# Seconds to wait between requests. Half a second sounds trivial but it is the
# difference between "a helpful indexer" and "a denial-of-service attack" from
# the server's point of view. Small sites often run on shared hosting that will
# fall over if you hammer it. Be a good citizen.
DELAY = 0.5          # be polite

# If a server does not respond within 15 seconds, give up on that page and move
# on. WITHOUT a timeout, `requests` will wait forever on a hung connection and
# your crawler silently freezes. Always set a timeout on network calls.
TIMEOUT = 15

# The User-Agent string identifies us to the server. Being honest about who you
# are (and giving a contact address) is best practice — a sysadmin who sees your
# traffic in their logs can email you instead of just blocking you.
# read more: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/User-Agent
UA = "CrawlerAI/0.1 (student project; contact: you@example.com)"

# The output folder. Path("pages") is relative to wherever you run the script.
OUT = Path("pages")


def same_domain(url, root_netloc):
    """
    Return True only if `url` lives on the same domain we started from.

    WHY THIS MATTERS
    ----------------
    Web pages link outward constantly — to Twitter, GitHub, a partner site, a
    CDN. If we followed those links we would attempt to crawl the entire
    internet, which is obviously not what a per-site index wants.

    "netloc" is urlparse's word for the network location, i.e. the host portion
    of a URL. For "https://openlake.in/projects?x=1" the netloc is "openlake.in".

    NOTE / KNOWN LIMITATION
    -----------------------
    This is an exact string comparison, so "openlake.in" and "www.openlake.in"
    are treated as DIFFERENT domains, and a subdomain like "blog.openlake.in"
    will not be crawled. That is intentionally strict here (predictable, no
    surprise expansion), and api.py separately normalises the "www." prefix away
    when it decides which index a query belongs to.
    """
    return urlparse(url).netloc == root_netloc


def is_html_url(url):
    """Cheap pre-filter: skip obvious non-HTML by extension.

    WHY "CHEAP" IS THE POINT
    ------------------------
    We can only know a URL's true type by looking at the Content-Type header,
    and getting that header means actually making the request. But a request to
    a 40MB PDF costs us bandwidth and seconds. So we do a free, zero-cost guess
    first based on the file extension in the path, and reject the obvious cases
    before spending a network round trip.

    This is a "fast path / slow path" design: a cheap approximate filter first,
    an exact-but-expensive check later (see the content-type check in crawl()).

    `str.endswith` accepts a TUPLE and returns True if the string ends with ANY
    of them, which is why `bad` is a tuple rather than a list.
    read more: https://docs.python.org/3/library/stdtypes.html#str.endswith
    """
    # .path gives us just "/files/report.pdf" without the domain or query
    # string. We lowercase it because "REPORT.PDF" is the same file as
    # "report.pdf" as far as this test is concerned.
    path = urlparse(url).path.lower()
    bad = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
           ".zip", ".mp4", ".mp3", ".css", ".js", ".ico", ".woff", ".woff2")
    return not path.endswith(bad)


def clean_url(url):
    """Drop #fragments so we don't crawl the same page 10 times.

    THE PROBLEM THIS SOLVES
    -----------------------
    A single page's own table of contents typically links to itself many times:
        /about
        /about#team
        /about#history
        /about#contact
    All four URLs return byte-for-byte identical HTML — the "#fragment" is
    handled entirely by the BROWSER (it scrolls to the matching element) and is
    never even sent to the server. Without this function our `seen` set would
    treat them as four distinct pages and we would download the same content
    four times, and produce four sets of duplicate chunks.

    `urldefrag` returns a 2-tuple (url_without_fragment, fragment); `[0]` takes
    the first element. We then `.rstrip("/")` so that "/about/" and "/about"
    normalise to the same string — trailing slashes are another classic source
    of accidental duplicates.

    read more: https://developer.mozilla.org/en-US/docs/Web/URI/Fragment
    """
    return urldefrag(url)[0].rstrip("/")


def slugify(url):
    """Turn a URL into a safe filename.

    WHY WE NEED THIS
    ----------------
    We want to save "https://site.com/blog/post-1" to disk, but a URL contains
    characters that are ILLEGAL in filenames on Windows: / : ? * " < > |
    So we transform the URL into a flat, safe, still-human-readable name:

        https://site.com/blog/post-1        ->  blog_post-1.html
        https://site.com/                   ->  index.html
        https://site.com/search?q=ai&p=2    ->  search_q-ai-p-2.html

    "Slug" is the standard web word for a short URL-safe version of a string.
    read more: https://developer.mozilla.org/en-US/docs/Glossary/Slug
    """
    p = urlparse(url)

    # p.path for "https://site.com/blog/post-1" is "/blog/post-1".
    #   .strip("/")        -> "blog/post-1"     (remove leading + trailing slash)
    #   .replace("/", "_") -> "blog_post-1"     (flatten the folder structure)
    # The trailing `or "index"` is a Python idiom: an empty string is "falsy",
    # so if the path was just "/" we end up with "" and fall back to "index".
    # That is how a homepage becomes index.html.
    slug = (p.path or "/").strip("/").replace("/", "_") or "index"

    # If the URL had a query string ("?q=ai&page=2"), fold it into the filename
    # too — otherwise /search?q=ai and /search?q=ml would collide into one file
    # and we would silently lose a page. \W+ means "one or more NON-word
    # characters" (anything that is not a letter, digit or underscore), so
    # "q=ai&page=2" becomes "q-ai-page-2".
    # read more: https://docs.python.org/3/library/re.html#regular-expression-syntax
    if p.query:
        slug += "_" + re.sub(r"\W+", "-", p.query)

    # Final safety net: replace ANY remaining character that is not a letter,
    # digit, dot, underscore or hyphen. Unicode paths, %20 escapes and other
    # oddities all get neutralised here.
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)

    # Truncate to 120 characters. Most filesystems cap a single filename at 255
    # bytes, and deeply nested URLs can easily exceed that. Truncation risks a
    # rare collision between two very long similar URLs, which we accept as a
    # trade for never crashing on a long path.
    return slug[:120] + ".html"


def load_robots(root):
    """Fetch and parse the site's /robots.txt.

    robots.txt is a plain text file at the root of a domain where the site owner
    declares which paths automated crawlers may and may not visit. It is a
    convention, not an enforced technical barrier — which is exactly why
    honouring it matters. It is the difference between a well-behaved indexer
    and a scraper.

    Returns a RobotFileParser we can later ask `.can_fetch(user_agent, url)`,
    or None if the file could not be read.

    read more:
      spec ....... https://www.rfc-editor.org/rfc/rfc9309.html
      friendly ... https://developers.google.com/search/docs/crawling-indexing/robots/intro
    """
    rp = RobotFileParser()

    # urljoin(root, "/robots.txt") correctly produces "https://site.com/robots.txt"
    # regardless of whether `root` was "https://site.com" or
    # "https://site.com/deep/page" — the leading slash means "from the domain
    # root". Doing this with string concatenation is a classic source of bugs.
    rp.set_url(urljoin(root, "/robots.txt"))

    try:
        rp.read()            # performs the actual HTTP fetch + parse
    except Exception:
        # A bare `except Exception` is usually a smell, but here it is correct
        # and deliberate: robots.txt failing is EXPECTED and non-fatal. It can
        # 404 (very common — most small sites have no robots.txt), time out,
        # return HTML instead of text, or have a broken TLS certificate. In
        # every one of those cases the correct interpretation per the standard
        # is "no restrictions stated", so we return None and crawl freely.
        return None          # no robots.txt = crawl allowed
    return rp


def crawl(root):
    """
    The main breadth-first crawl loop.

    WHY BREADTH-FIRST AND NOT DEPTH-FIRST?
    --------------------------------------
    BFS visits every page 1 hop away before any page 2 hops away. DFS would
    dive down one branch as deep as it can before backtracking.

    BFS is the right choice for a site indexer because the most IMPORTANT pages
    on a website are almost always the closest to the homepage — About, Team,
    Projects, Contact are all one click away, while depth-3 pages tend to be
    individual blog posts and archive pages. When we hit MAX_PAGES and stop, BFS
    guarantees we stopped having already collected the important stuff. DFS
    might have spent its whole budget inside one blog archive.

    read more: https://en.wikipedia.org/wiki/Breadth-first_search
    """
    # Normalise the starting URL the same way we will normalise every discovered
    # link, so that the root can be compared against them consistently.
    root = clean_url(root)

    # Remember the host we started on. Every discovered link is tested against
    # this so we never wander off-site.
    root_netloc = urlparse(root).netloc

    rp = load_robots(root)

    # exist_ok=True means "do not raise an error if the folder already exists".
    # Without it, a second run of the script would crash with FileExistsError.
    OUT.mkdir(exist_ok=True)

    # A Session keeps the underlying TCP connection alive between requests
    # (HTTP keep-alive) instead of doing a fresh DNS lookup + TCP handshake +
    # TLS handshake for every single page. Over 200 pages that is a large
    # speedup for one line of code. It also lets us set headers once, below.
    # read more: https://requests.readthedocs.io/en/latest/user/advanced/#session-objects
    session = requests.Session()
    session.headers["User-Agent"] = UA

    # --- the three pieces of BFS state -------------------------------------
    # `seen`: a SET of every URL we have ever queued. A set gives O(1) membership
    #   testing (`x in seen`), where a list would be O(n) — with thousands of
    #   URLs that difference is the whole runtime. Critically, we add to `seen`
    #   when we ENQUEUE a URL, not when we fetch it, so a page linked from five
    #   other pages is still only ever queued once.
    #   read more: https://wiki.python.org/moin/TimeComplexity
    seen = {root}

    # `queue`: the BFS frontier. Holds (url, depth) tuples so each page carries
    #   its own distance-from-home with it. We seed it with the root at depth 0.
    queue = deque([(root, 0)])

    # `index`: the results we will write to index.json at the end.
    index = []

    # Loop while there is work left AND we are under the page budget. Both
    # conditions matter: `queue` empties on a small site, MAX_PAGES catches a
    # huge one.
    while queue and len(index) < MAX_PAGES:
        # popleft() takes from the FRONT of the deque. This single choice is
        # what makes the algorithm breadth-first. If we used .pop() (from the
        # right) the exact same code would become a depth-first search.
        url, depth = queue.popleft()

        # `rp and ...` short-circuits: if load_robots returned None, Python never
        # evaluates the second half and the page is allowed.
        if rp and not rp.can_fetch(UA, url):
            print(f"[robots] skip {url}")
            continue                    # `continue` = skip to the next URL

        try:
            r = session.get(url, timeout=TIMEOUT)
        except Exception as e:
            # One dead link must never kill an entire crawl. DNS failures,
            # connection resets, timeouts and TLS errors are all normal on the
            # real web. We log it and carry on.
            print(f"[error] {url} -> {e}")
            continue

        # --- the accurate (but expensive) type check ------------------------
        # `is_html_url` was our free guess based on the extension. NOW we have
        # the server's authoritative answer in the Content-Type header, so we
        # use it. This catches things the extension check cannot: an API
        # endpoint at "/api/data" returning JSON, an image served from an
        # extension-less URL, a redirect to a login page, and so on.
        #
        # .get(name, default) is used rather than [name] because a malformed
        # response might omit the header entirely, and [name] would raise.
        ctype = r.headers.get("content-type", "")

        # A real Content-Type often looks like "text/html; charset=utf-8", so we
        # test with `in` rather than `==`.
        if r.status_code != 200 or "text/html" not in ctype:
            # ctype.split(';')[0] trims "; charset=utf-8" off for a tidier log.
            print(f"[skip]  {url} ({r.status_code}, {ctype.split(';')[0]})")
            continue

        # Parse the HTML into a navigable tree. "html.parser" is Python's
        # built-in parser — slower than lxml but with zero extra dependencies,
        # which matters because this project must fit in a 512MB deployment.
        # read more: https://www.crummy.com/software/BeautifulSoup/bs4/doc/#installing-a-parser
        soup = BeautifulSoup(r.text, "html.parser")

        # Extract the <title>. The `if soup.title else ""` guard is essential —
        # a page with no <title> tag makes `soup.title` None, and calling
        # .get_text() on None raises AttributeError. Defensive parsing like this
        # is most of what real-world HTML handling consists of.
        title = soup.title.get_text(strip=True) if soup.title else ""

        # --- save the page -----------------------------------------------
        fname = slugify(url)
        # encoding="utf-8" is explicitly specified because Python on Windows
        # otherwise defaults to the system codepage (cp1252), which cannot
        # represent most non-English characters and will crash on them. Always
        # be explicit about encoding when writing text files.
        # read more: https://docs.python.org/3/howto/unicode.html
        (OUT / fname).write_text(r.text, encoding="utf-8")

        index.append({"url": url, "file": fname, "title": title, "depth": depth})

        # f-string format specifiers keeping the log readable as a table:
        #   {len(index):3d}  -> integer right-aligned in 3 columns
        #   {title[:45]:45s} -> string truncated AND padded to exactly 45 chars
        # read more: https://docs.python.org/3/library/string.html#format-specification-mini-language
        print(f"[ok {len(index):3d}] d{depth} {title[:45]:45s} {url}")

        # --- discover new links -------------------------------------------
        # Only look for more links if we have depth budget left. At MAX_DEPTH we
        # still SAVE the page, we just do not follow its links any further.
        if depth < MAX_DEPTH:
            # href=True means "only <a> tags that actually have an href
            # attribute" — this skips anchor tags used purely as JS hooks.
            for a in soup.find_all("a", href=True):
                # urljoin resolves whatever form the href took — "/about",
                # "../team", "contact.html" or a full "https://..." — into an
                # absolute URL, relative to the page we found it on.
                nxt = clean_url(urljoin(url, a["href"]))

                # Four independent gates, all of which must pass:
                if (nxt not in seen                 # 1. never queued before
                        and same_domain(nxt, root_netloc)   # 2. still our site
                        and is_html_url(nxt)                # 3. probably HTML
                        and nxt.startswith("http")):        # 4. real web URL,
                    #    which rejects "mailto:", "tel:", "javascript:void(0)"
                    #    and "data:" links that urljoin passes through unchanged.

                    # Mark as seen at ENQUEUE time, not fetch time — see the
                    # note on `seen` above.
                    seen.add(nxt)
                    # Children are one hop further from home than their parent.
                    queue.append((nxt, depth + 1))

        # The politeness pause. Placed at the very END of the loop body so it
        # only ever runs after a successful fetch (the `continue` statements
        # above skip it), meaning we do not sleep for pages we never downloaded.
        time.sleep(DELAY)

    # --- write the manifest ------------------------------------------------
    # index.json is the bridge between this file and chunker.py. The saved .html
    # files alone are not enough: a filename like "blog_post-1.html" does not
    # tell you the original URL, and the widget needs the real URL to navigate
    # the visitor there. indent=2 makes the file readable when you open it.
    (OUT / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"\nDone. {len(index)} pages -> {OUT}/  (see index.json)")


# This is the standard Python "script vs library" guard. __name__ is set to
# "__main__" only when the file is RUN directly (python crawler.py ...). If some
# other file does `import crawler`, __name__ becomes "crawler" and this block is
# skipped — so importing the module never accidentally triggers a full crawl.
# read more: https://docs.python.org/3/library/__main__.html
if __name__ == "__main__":
    # sys.argv is the list of command-line words. sys.argv[0] is always the
    # script name itself, so a single expected argument means a length of 2.
    if len(sys.argv) != 2:
        # Passing a string to sys.exit() prints it to stderr and exits with
        # status code 1 (failure) — a neat one-liner for CLI usage errors.
        sys.exit("usage: python crawler.py <root-url>")
    crawl(sys.argv[1])
