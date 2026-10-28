"""
===============================================================================
 check_links.py — crawl coverage audit (a debugging tool, not a pipeline stage)
===============================================================================

WHAT IT DOES
------------
Compares two sets:

    A = every link that APPEARS anywhere in the crawled pages
    B = every page the crawler actually DOWNLOADED

and prints A - B: the links we saw but never followed.

WHY THIS TOOL MATTERS MORE THAN IT LOOKS
----------------------------------------
A crawler's worst failure mode is SILENT INCOMPLETENESS. It does not crash, it
does not warn — it just quietly returns 8 pages when the site has 30. Every
downstream stage then works perfectly on incomplete data, and the only symptom
is that Compass "can't find" things that plainly exist on the site. That is a
miserable bug to chase from the far end of the pipeline.

This script turns that invisible failure into a printed list.

WHAT THE OUTPUT TELLS YOU
-------------------------
Every line in "NOT crawled" falls into one of these categories, and the netloc
in brackets is what lets you tell them apart at a glance:

  a DIFFERENT domain      -> correct and expected. same_domain() rejected it.
                             (Twitter, GitHub, a partner site.)
  a .pdf / .jpg           -> correct. is_html_url() rejected it.
  "mailto:" / "tel:"      -> correct. The startswith("http") gate rejected it.
  OUR OWN domain, HTML    -> *** THIS IS A BUG. *** We should have crawled it
                             and did not.

THE REAL DISCOVERY THIS TOOL MADE
---------------------------------
On openlake.in, running this showed a cluster of missing same-domain pages that
turned out to sit behind hover dropdown menus — their <a> tags were never in the
DOM at crawl time, because the menu only renders on hover. That finding is the
direct reason crawler_js.py contains the hover loop, and part of the reason it
exists at all.

So this file is a good illustration of a general habit: when a pipeline stage
might be silently lossy, write the small script that measures the loss.

READ MORE
---------
  Set operations ..... https://docs.python.org/3/tutorial/datastructures.html#sets
  urljoin ............ https://docs.python.org/3/library/urllib.parse.html#urllib.parse.urljoin
  Crawl budget ....... https://developers.google.com/search/docs/crawling-indexing/large-site-managing-crawl-budget

Usage:
    python check_links.py      (after a crawl has populated pages/)
"""

# check_links.py
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

# index.json is the crawler's manifest of what it actually fetched.
index = json.loads(Path("pages/index.json").read_text(encoding="utf-8"))

# A SET COMPREHENSION building the "what we crawled" side of the comparison.
# `.rstrip("/")` normalises trailing slashes, matching what clean_url() did
# during the crawl — without this, "/about" and "/about/" would look like two
# different URLs and the report would be full of false positives.
# A set is the right structure because the only operation we need is fast
# membership testing.
crawled = {p["url"].rstrip("/") for p in index}

# `found` maps  absolute_url -> the raw href exactly as it was written in the
# HTML. Keeping the RAW form alongside the resolved one is what makes the report
# actually diagnosable: seeing that "/team" resolved to "https://other.com/team"
# instantly tells you a <base> tag or an absolute href is at play, which you
# could never work out from the final URL alone.
found = {}

for page in index:
    html = Path("pages") / page["file"]
    soup = BeautifulSoup(html.read_text(encoding="utf-8"), "html.parser")

    for a in soup.find_all("a", href=True):
        raw = a["href"]

        # Resolve relative to the page it was found on — the same call the
        # crawler made. Using the identical resolution logic is what makes this
        # a valid audit rather than an approximation.
        full = urljoin(page["url"], raw)

        # `.setdefault` records the FIRST raw form seen for each resolved URL and
        # leaves it alone thereafter. A plain assignment would keep overwriting,
        # so you would end up reporting whichever page happened to be parsed
        # last — arbitrary and confusing.
        found.setdefault(full, raw)

print(f"crawled: {len(crawled)} pages")
print(f"unique links seen: {len(found)}\n")

# The gap between these two numbers is the headline. A large gap is normal
# (external links, images, mailto:), but it is the CONTENT of the gap that
# matters — which is what the loop below prints.
print("--- NOT crawled ---")

# `sorted(found.items())` sorts by URL, which conveniently groups links from the
# same domain together and makes the same-domain block — the one you actually
# care about — easy to spot.
for full, raw in sorted(found.items()):
    if full.rstrip("/") not in crawled:
        # Extracting and printing the netloc is the key affordance of this
        # report: it lets you triage each line in a fraction of a second.
        # If the netloc is YOUR site and the path looks like a page, that line
        # is a genuine gap in coverage and worth investigating.
        netloc = urlparse(full).netloc

        # `{raw:45s}` pads the raw href to a fixed 45 columns so the arrows and
        # the resolved URLs line up into readable columns.
        print(f"  raw={raw:45s}  ->  {full}   [{netloc}]")
