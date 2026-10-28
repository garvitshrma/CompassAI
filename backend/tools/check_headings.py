"""
===============================================================================
 check_headings.py — a debugging tool, not a pipeline stage
===============================================================================

WHAT IT DOES
------------
Walks every crawled page in pages/ and prints its headings, with their ids.

WHY IT EXISTS — THE DIAGNOSTIC QUESTION IT ANSWERS
--------------------------------------------------
chunker.py splits pages at h1/h2/h3. So when a site indexes badly, there are two
very different possible causes, and they need opposite fixes:

    A) THE CHUNKER IS WRONG — the headings are there, but our filtering
       (in_chrome, MIN_CHARS, the h3 rule) is discarding them.
       -> fix chunker.py

    B) THERE ARE NO HEADINGS — the crawler never captured any, because the site
       is a JavaScript app and `requests` only saw an empty shell, or because
       the designer styled <div>s to look like headings.
       -> fix the CRAWL (use crawler_js.py), or accept the text-window fallback

Guessing between those wastes hours. This script answers it in one second, by
showing you exactly what the chunker had to work with.

That is the general lesson worth taking from this file: in a multi-stage
pipeline, the most valuable tool you can write is usually the one that lets you
SEE the intermediate state. A twelve-line script that makes a failure obvious
pays for itself immediately.

WHAT TO LOOK FOR IN THE OUTPUT
------------------------------
  "(0 headings)"          -> cause (B). The page is JS-rendered, or empty.
                             This is what motivated crawler_js.py, and the
                             _chunk_text_fallback path in indexer.py.
  many headings, all nav  -> in_chrome() is doing its job; the real content is
                             elsewhere or missing.
  "id=—" everywhere       -> no ids, so css_path() must generate long
                             :nth-of-type() selectors, which are more brittle.
                             Useful to know before you debug a failing scroll.

READ MORE
---------
  Heading elements ... https://developer.mozilla.org/en-US/docs/Web/HTML/Element/Heading_Elements
  Document outline ... https://www.w3.org/WAI/tutorials/page-structure/headings/
  pathlib.glob ....... https://docs.python.org/3/library/pathlib.html#pathlib.Path.glob

Usage:
    python check_headings.py
"""

# inspect.py
from pathlib import Path
from bs4 import BeautifulSoup

# `.glob("*.html")` yields every HTML file in pages/ — note it does NOT recurse
# into subdirectories (that would be "**/*.html" with rglob), so it sees the
# flat layout crawler.py produces, not crawler_js.py's per-domain folders.
# `sorted()` gives a stable, alphabetical order so two runs are comparable and
# you can diff the output after a change.
for f in sorted(Path("pages").glob("*.html")):
    soup = BeautifulSoup(f.read_text(encoding="utf-8"), "html.parser")

    # Deliberately the SAME list chunker.py uses. If these ever drifted apart,
    # this tool would be reporting on something other than what the chunker
    # actually sees, which is worse than having no tool at all.
    heads = soup.find_all(["h1", "h2", "h3"])

    # The count in the header line is the single most informative number here —
    # "(0 headings)" immediately identifies a JS-rendered page.
    print(f"\n=== {f.name}  ({len(heads)} headings)")

    # Only the first 12 per page. A long page could have fifty headings, and the
    # goal is a quick scan across MANY pages, not an exhaustive dump of one.
    for h in heads[:12]:
        # `h.get("id")` returns None when the attribute is absent, and `or "—"`
        # substitutes a visible dash so the columns stay aligned and "no id" is
        # obvious at a glance rather than reading as the word "None".
        #
        # WHY THE ID IS WORTH PRINTING: css_path() returns "#the-id" when one
        # exists, which is a short, stable selector. Without ids it must build a
        # long positional path that breaks if the page structure shifts. So this
        # column is a direct preview of how robust this page's navigation will be.
        hid = h.get("id") or "—"

        # Truncated to 60 chars to keep one heading per terminal line.
        print(f"  <{h.name} id={hid}> {h.get_text(strip=True)[:60]}")
