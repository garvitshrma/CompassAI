"""
===============================================================================
 register_site.py — the admin CLI: crawl locally, upload the result
===============================================================================

register_site.py — crawl a site locally and push it to the Compass API.

Runs on YOUR machine (or any box with memory to spare), so Playwright's
headless browser never has to fit inside the API's 512MB. The API only
receives the finished chunks.

-------------------------------------------------------------------------------
 THE ARCHITECTURAL IDEA: SPLIT THE HEAVY WORK OFF THE SERVER
-------------------------------------------------------------------------------
Indexing has two halves with wildly different resource profiles:

    CRAWL + CHUNK        heavy  — a headless Chromium is ~300MB+ of RAM
    EMBED + SERVE        light  — a 90MB ONNX model and some NumPy

Render's free tier gives us 512MB total. Chromium simply does not fit. Rather
than pay for a bigger box, we move the heavy half to a machine that already has
plenty of memory: your laptop. The server receives only the finished JSON.

This is a common and genuinely good pattern — do the expensive, occasional,
offline work wherever it is cheap, and keep the always-on service small. The
same logic explains /ingest in api.py, which pushes the rendering cost even
further out, all the way to the visitor's own browser.

    read more:
      argparse .......... https://docs.python.org/3/library/argparse.html
      subprocess ........ https://docs.python.org/3/library/subprocess.html
      requests .......... https://requests.readthedocs.io/en/latest/
      12-factor config .. https://12factor.net/config

Usage:
    # full pipeline: crawl -> chunk -> upload
    python register_site.py https://example.com

    # skip the crawl, upload an existing chunks.json
    python register_site.py https://example.com --chunks chunks.json

    # target a deployed API instead of localhost
    python register_site.py https://example.com --api https://compassai-vkoe.onrender.com

Needs ADMIN_TOKEN in .env (must match the API's ADMIN_TOKEN).
"""

import argparse      # standard-library command-line argument parsing
import json
import subprocess    # to run crawler_js.py and chunker.py as separate processes
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
import os

# Read .env into os.environ. Keeps ADMIN_TOKEN out of the source and out of git
# (.env is gitignored) while still being trivially available here.
load_dotenv()


def normalize_site_id(raw):
    """
    Reduce a URL to the canonical site key.

        "https://www.Example.com:8080/pricing"  ->  "example.com"

    IMPORTANT: this is a deliberate DUPLICATE of the function in api.py, and the
    two must stay in agreement. If they ever disagree, this script would upload
    an index under one key while the widget queries a different one, and the
    site would silently appear unregistered.

    Duplicating it avoids importing api.py here (which would drag in FastAPI,
    the embedding model and a Groq client just to run a CLI). The safer long-term
    fix is to move this one function into a shared `common.py` that both import.
    Knowing about this kind of trade-off — and where it might bite — matters more
    than the four lines themselves.
    """
    s = (raw or "").strip().lower()
    s = s.split("//")[-1]          # drop the scheme
    s = s.split("/")[0]            # drop the path
    s = s.split(":")[0]            # drop the port
    if s.startswith("www."):
        s = s[4:]
    return s


def crawl_and_chunk(url):
    """Run the existing crawler + chunker as separate steps.

    WHY subprocess RATHER THAN JUST IMPORTING THEM?
    Both scripts are written as command-line tools, with their configuration in
    module-level constants and their entry point behind `if __name__ ==
    "__main__"`. Shelling out reuses them exactly as they are, with no
    refactoring, and gives us process isolation for free — Playwright leaves
    behind browser processes and a fair amount of memory, and a crashing crawl
    cannot take this script down with it.

    The honest downside is that we can only observe the exit code, not intercept
    the work. For a tool you run by hand a few times a week, that is fine.
    """
    print(f"\n[1/3] crawling {url} ...")

    # `sys.executable` is the full path to the Python interpreter CURRENTLY
    # running. Using it instead of the literal string "python" guarantees the
    # subprocess uses the same virtual environment — otherwise you can hit the
    # maddening situation where the subprocess runs a system Python that has no
    # Playwright installed and fails with ImportError.
    # read more: https://docs.python.org/3/library/sys.html#sys.executable
    r = subprocess.run([sys.executable, "crawler_js.py", url])

    # Exit code 0 means success by universal Unix convention; anything else is a
    # failure. Stopping here rather than continuing means we never upload a
    # half-crawled site over a good existing index.
    if r.returncode != 0:
        sys.exit("crawl failed")

    # ---- bridge the folder-layout mismatch --------------------------------
    # crawler_js.py writes to  pages/<domain>/
    # chunker.py reads from    pages/
    # So we have to stage this crawl's files one level up before chunking.
    # This is glue code papering over an inconsistency between two scripts that
    # were written at different times — a small, real piece of technical debt,
    # and worth naming as such rather than pretending it is by design.

    # The `if "//" in url else` guard matters: urlparse("example.com") puts the
    # whole string in .path and leaves .netloc EMPTY, because without a scheme
    # it cannot tell a host from a path. Adding "https://" first makes it parse
    # correctly. A classic urlparse gotcha.
    netloc = urlparse(url if "//" in url else "https://" + url).netloc
    src = Path("pages") / netloc.replace(":", "_")

    # Verify the crawl actually produced something before proceeding, so the
    # failure message names the real problem instead of surfacing later as a
    # confusing FileNotFoundError from inside chunker.py.
    if not (src / "index.json").exists():
        sys.exit(f"no index.json in {src} — did the crawl find anything?")

    # chunker.py reads pages/index.json, so stage this crawl there
    print(f"[2/3] chunking {src} ...")
    dest = Path("pages")
    for f in src.iterdir():
        # read_bytes/write_bytes rather than read_text/write_text: this copies
        # raw bytes with no encoding interpretation at all, so a page in an
        # unusual charset cannot be mangled in transit. When you are only moving
        # a file, never decode it.
        (dest / f.name).write_bytes(f.read_bytes())

    r = subprocess.run([sys.executable, "chunker.py"])
    if r.returncode != 0:
        sys.exit("chunking failed")

    return Path("chunks.json")


def upload(api, site_id, chunks_path, token):
    """POST the finished chunks to the API's admin-only /register endpoint."""
    chunks = json.loads(Path(chunks_path).read_text(encoding="utf-8"))
    print(f"[3/3] uploading {len(chunks)} chunks as '{site_id}' -> {api}")

    resp = requests.post(
        # `.rstrip("/")` prevents the classic double-slash bug: if someone passes
        # --api "https://x.com/" we would otherwise build "https://x.com//register",
        # which some servers 404 on.
        api.rstrip("/") + "/register",

        # `json=` (rather than `data=`) makes requests serialise the dict AND set
        # the Content-Type: application/json header automatically.
        json={"site_id": site_id, "chunks": chunks},

        # The shared-secret header that api.py's register() checks. Sent as a
        # header rather than in the body or the URL, because URLs get written to
        # access logs and browser history — headers generally do not.
        headers={"X-Admin-Token": token},

        # FIVE MINUTES, far longer than a normal API call. Justified: the server
        # must embed every chunk on receipt, which for a large site genuinely
        # takes minutes on a small free-tier CPU. The default would time out
        # long before the work finished, and you would wrongly conclude it broke.
        timeout=300,
    )
    if resp.status_code != 200:
        # Show the status AND a slice of the body. The body is where FastAPI puts
        # the useful `detail` message ("invalid admin token", "chunks missing
        # fields: [...]"); [:300] keeps an HTML error page from flooding the
        # terminal.
        sys.exit(f"register failed [{resp.status_code}]: {resp.text[:300]}")

    print("done:", resp.json())


def main():
    """Parse arguments, check config, run the pipeline."""
    # argparse gives us --flags, type checking, and an automatic --help screen
    # for free. Always prefer it over picking through sys.argv by hand.
    p = argparse.ArgumentParser()

    # A POSITIONAL argument — required, no flag needed.
    p.add_argument("url", help="site to register, e.g. https://example.com")

    # Optional with a default. Defaulting to localhost is the safe choice: the
    # dangerous action (writing to the live production index) requires you to
    # type it out explicitly, so you cannot overwrite a customer's index by
    # forgetting a flag.
    p.add_argument("--api", default="http://localhost:8000")

    # An escape hatch for the common case where the crawl already ran and you
    # only want to retry the upload — or where you hand-edited chunks.json.
    # Not having to re-crawl a 200-page site to fix a typo is worth the flag.
    p.add_argument("--chunks", help="skip crawling, upload this chunks.json")

    args = p.parse_args()

    # CHECK CONFIG BEFORE DOING ANY WORK. Discovering the token is missing after
    # a five-minute Playwright crawl would be infuriating. Validate cheap
    # preconditions first, always.
    token = os.environ.get("ADMIN_TOKEN")
    if not token:
        sys.exit("Set ADMIN_TOKEN in .env (must match the API's).")

    site_id = normalize_site_id(args.url)

    # A conditional expression choosing between the two paths: use the supplied
    # chunks.json, or run the full crawl-and-chunk pipeline to produce one.
    chunks_path = Path(args.chunks) if args.chunks else crawl_and_chunk(args.url)

    upload(args.api, site_id, chunks_path, token)


if __name__ == "__main__":
    main()
