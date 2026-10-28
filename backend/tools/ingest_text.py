"""
===============================================================================
 tools/ingest_text.py — turn a plain text file into a queryable Compass index.
===============================================================================

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
This is NOT a shortcut around the crawler. It is the "owner-authored facts"
feature, arriving as a file instead of a dashboard form.

Some of the most valuable things a site owner wants the bot to know are not on
any page: the fee that is only quoted over email, who to contact about
admissions, the fact that the office is closed on Fridays. Those have no URL and
no CSS selector, because they have no DOM element. They still need to be
answerable.

So this produces chunks in EXACTLY the same shape the crawler produces, with two
differences:

    selector : None    — there is nothing to scroll to
    source   : "manual" — so you can tell crawled from hand-written

Everything downstream — embedding, hybrid retrieval, the confidence floor, the
LLM grounding, the answer cache — works unchanged, because none of it ever cared
where a chunk came from. That is the payoff of having kept `core/` free of
assumptions about the crawler.

WHAT YOU LOSE: navigation. No selector means no scroll-and-highlight. If you
supply a `url:` line the widget can still offer "Take me there →" and land the
visitor on the right PAGE, just not the right paragraph. Page-level navigation
is most of the value for a fact that is a sentence long anyway.

-------------------------------------------------------------------------------
 THE FILE FORMAT
-------------------------------------------------------------------------------
    title: OpenLake                     <- optional, once, at the top
                                           becomes page_title on every chunk

    # Fees and membership               <- a heading starts a new chunk (level h1)
    OpenLake is free to join.           <- body text until the next heading
    There is no membership fee.

    ## Who can join                     <- ## is level h2, ### is h3
    url: https://openlake.in/join       <- optional, first line under a heading
    Any IIT Bhilai student can join.

Rules, kept few on purpose:
  * A heading line starts a chunk. Text before the first heading is ignored
    (apart from `title:`), so you can put notes at the top of the file.
  * `url:` must be the first line under its heading, or it is treated as body.
  * Blank lines are preserved inside a chunk's body and stripped at the ends.

-------------------------------------------------------------------------------
 USAGE
-------------------------------------------------------------------------------
    # from the backend/ folder, with the server running:
    python tools/ingest_text.py facts.txt --site openlake-facts

    # preview without sending anything:
    python tools/ingest_text.py facts.txt --site openlake-facts --dry-run

WARNING, READ THIS ONCE:
/register REPLACES a site's entire index. If you point this at a site_id that
already holds crawled chunks, those chunks are GONE — this is not a merge. Use a
separate site_id (e.g. "openlake-facts") while you are experimenting. Merging
hand-written facts with crawled content is a real feature, and it belongs in the
database work, not in a one-off script that can silently destroy an index.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# python-dotenv so ADMIN_TOKEN is read from backend/../.env exactly as the
# server reads it. Without this you would have to export the token by hand.
from dotenv import load_dotenv

import requests

# This file is backend/tools/ingest_text.py, so two .parent hops reach backend/,
# and the .env sits one above that at the repo root. Anchored to __file__ for the
# same reason core/retrieval.py is — so it does not matter where you run it from.
BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR.parent / ".env")

# A heading line: one to six leading '#', a space, then the text.
# ^\s* tolerates accidental leading whitespace, which is the single most common
# way a hand-written file fails to parse.
HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*\S)\s*$")

# `url:` or `URL:` followed by something that looks like a link.
URL_RE = re.compile(r"^\s*url\s*:\s*(\S+)\s*$", re.IGNORECASE)

TITLE_RE = re.compile(r"^\s*title\s*:\s*(.*\S)\s*$", re.IGNORECASE)


def parse(text: str, default_title: str):
    """Text in, list of chunk dicts out. No network, no I/O — easy to test."""
    lines = text.splitlines()

    page_title = default_title
    chunks = []
    current = None          # the chunk being built, or None before the first heading

    for raw in lines:
        # ---- title:, but only before the first heading --------------------
        if current is None:
            m = TITLE_RE.match(raw)
            if m:
                page_title = m.group(1)
                continue

        # ---- a heading starts a new chunk ---------------------------------
        m = HEADING_RE.match(raw)
        if m:
            if current is not None:
                chunks.append(current)
            hashes, heading = m.group(1), m.group(2)
            current = {
                "heading": heading,
                # "h1".."h6" — the same representation the crawler uses, so
                # retrieval.py's h1 level bonus keeps working untouched.
                "level": f"h{len(hashes)}",
                "url": "",
                "body": [],
            }
            continue

        # ---- anything before the first heading is a comment ---------------
        if current is None:
            continue

        # ---- url:, only as the first content line under its heading -------
        # The `not current["body"]` guard is what makes "first line" true. A
        # later line that happens to start with "url:" is body text, which is
        # what you would want if a fact is literally about a URL.
        if not current["body"]:
            m = URL_RE.match(raw)
            if m:
                current["url"] = m.group(1)
                continue

        current["body"].append(raw)

    if current is not None:
        chunks.append(current)

    # ---- finalise into the schema /register validates ---------------------
    out = []
    for i, c in enumerate(chunks):
        # "\n".join then .strip() collapses leading/trailing blank lines but
        # keeps paragraph breaks inside the body.
        content = "\n".join(c["body"]).strip()

        # A heading with no body is a section divider, not a fact. Embedding it
        # would add a chunk that can win a similarity match and then have
        # nothing to say — a confident empty answer, which is worse than no
        # answer. So it is dropped, loudly.
        if not content:
            print(f"  skipped (no body): {c['heading']}", file=sys.stderr)
            continue

        out.append({
            # `id` MUST equal this chunk's row index in the embeddings matrix.
            # It is assigned here, after filtering, for that reason — see the
            # renumbering note in core/chunker.py.
            "id": len(out),
            "url": c["url"],
            "heading": c["heading"],
            "selector": None,          # nothing to scroll to; this is the point
            "content": content,
            "level": c["level"],
            "page_title": page_title,
            "source": "manual",        # lets you tell these from crawled chunks
        })
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Index a plain text file as a Compass site.")
    ap.add_argument("file", help="the .txt or .md file to ingest")
    ap.add_argument("--site", required=True,
                    help="site_id to register under. USE A FRESH ONE: this "
                         "REPLACES that site's whole index.")
    ap.add_argument("--api", default="http://127.0.0.1:8000",
                    help="base URL of the Compass API (default localhost:8000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and print, send nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"no such file: {path}")

    text = path.read_text(encoding="utf-8")
    chunks = parse(text, default_title=path.stem)

    if not chunks:
        sys.exit("no chunks produced — does the file have '# Heading' lines "
                 "with text under them?")

    print(f"\nparsed {len(chunks)} chunk(s) from {path.name}:\n")
    for c in chunks:
        nav = f"  -> {c['url']}" if c["url"] else ""
        print(f"  [{c['id']:3}] {c['level']}  {c['heading'][:50]:50}"
              f"  {len(c['content']):5} chars{nav}")

    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        print(json.dumps(chunks[0], indent=2, ensure_ascii=False))
        return

    token = os.environ.get("ADMIN_TOKEN")
    if not token:
        sys.exit("\nADMIN_TOKEN is not set. /register requires it — add it to "
                 ".env at the repo root.")

    print(f"\nPOSTing to {args.api}/register as site '{args.site}'...")
    print("NOTE: this REPLACES that site's entire index.")

    r = requests.post(
        f"{args.api}/register",
        json={"site_id": args.site, "chunks": chunks},
        headers={"X-Admin-Token": token},
        # Generous because the server embeds every chunk before replying, and a
        # cold server also has to load the ONNX model first.
        timeout=300,
    )

    if r.status_code != 200:
        sys.exit(f"failed [{r.status_code}]: {r.text}")

    print("ok:", r.json())
    print(f"\nTry it:\n  Invoke-RestMethod -Uri {args.api}/query -Method Post "
          f"-ContentType application/json "
          f"-Body '{{\"query\":\"...\",\"site_id\":\"{args.site}\"}}'")


if __name__ == "__main__":
    main()
