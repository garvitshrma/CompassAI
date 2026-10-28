"""
===============================================================================
 smoke_test.py  —  does the query pipeline still work, and how fast?
===============================================================================

Runs a fixed set of queries against ONE site and prints what came back, how
long it took, and whether the refusals landed where they should.

WHY THIS EXISTS AS A SCRIPT AND NOT A NOTE IN THE README
--------------------------------------------------------
The two things that break this project are both invisible to a normal unit test:

  * A hosted model id gets decommissioned by the provider. Nothing in the repo
    changed, every test still passes, and production returns 404 on every query.
  * Retrieval quietly starts ranking a worse chunk first. No exception, no
    failure — just a visitor sent to the wrong section.

Neither shows up unless you actually run real queries and LOOK at the answers.
So this script optimises for being run by a human who reads the output, rather
than for a green/red CI signal.

The EXPECT column encodes intent: "found" means a good destination should exist,
"refuse" means the site genuinely has no answer and saying so is the correct
behaviour. A refusal in a "found" row, or an answer in a "refuse" row, is the
signal worth investigating.

Usage:
    # against the library directly (no server needed) -- fastest feedback
    python smoke_test.py

    # against a running server, to test the real HTTP path + the answer cache
    python smoke_test.py --http

    # a different site
    python smoke_test.py --site gdg-iitbh.vercel.app
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv
load_dotenv()

# (query, expectation) -- see the note above on what EXPECT means.
# The "refuse" rows matter as much as the "found" ones: a navigation product
# that answers everything is a product that lies, so unanswerable queries are
# first-class test cases, not an afterthought.
QUERIES = [
    ("where are the projects",       "found"),
    ("who are the coaches",          "found"),
    ("how do i join",                "found"),
    ("what is canonforces",          "found"),
    ("campus marketplace",           "found"),
    ("what is the weather in paris", "refuse"),
    ("do you sell pizza",            "refuse"),
    ("asdfghjkl",                    "refuse"),
]


def run_direct(site):
    """Call answer() in-process. No server, no HTTP, no cache."""
    from groq import Groq
    from retrieval import load_site
    from answer import answer, LLM_MODEL, CONFIDENCE_FLOOR, N_CANDIDATES

    key = os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY is not set (put it in .env)")

    print(f"model={LLM_MODEL}  floor={CONFIDENCE_FLOOR}  candidates={N_CANDIDATES}")

    # Same client settings as api.py builds at startup, so the timings here are
    # representative rather than optimistic.
    llm = Groq(api_key=key, timeout=8.0, max_retries=1)

    # The first call loads the ONNX embedder (~90MB). Doing it before the timing
    # loop keeps that one-off cost out of the per-query numbers -- exactly the
    # warm-up api.py's lifespan does for the same reason.
    t = time.perf_counter()
    chunks, vecs = load_site(site)
    print(f"loaded {len(chunks)} chunks in {time.perf_counter() - t:.2f}s\n")

    return lambda q: answer(q, chunks, vecs, llm)


def run_http(site, base):
    """Call a running server over HTTP. Tests routing, serialisation and cache."""
    import urllib.request
    import json as _json

    def ask(q):
        req = urllib.request.Request(
            f"{base}/query",
            data=_json.dumps({"query": q, "site_id": site}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return _json.load(r)

    try:
        with urllib.request.urlopen(f"{base}/health", timeout=10) as r:
            import json as j
            print(f"server up: {j.load(r)}\n")
    except Exception as e:
        sys.exit(f"cannot reach {base} -- is uvicorn running?  ({e})")
    return ask


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--site", default="openlake.in")
    p.add_argument("--http", action="store_true",
                   help="test a running server instead of calling answer() directly")
    p.add_argument("--base", default="http://127.0.0.1:8000")
    args = p.parse_args()

    ask = run_http(args.site, args.base) if args.http else run_direct(args.site)

    total = 0.0
    surprises = []

    for query, expect in QUERIES:
        t = time.perf_counter()
        r = ask(query)
        dt = time.perf_counter() - t
        total += dt

        got = "found" if r.get("found") else "refuse"
        # "!!" marks a result that contradicts the expectation. It is a prompt to
        # go and look, not necessarily a bug -- a site's content legitimately
        # changes, and then the expectation is what needs updating.
        flag = "  " if got == expect else "!!"
        if flag == "!!":
            surprises.append(query)

        if r.get("found"):
            print(f"{flag} {dt:6.2f}s  {query:30s} -> {str(r['heading'])[:34]:34s} "
                  f"conf={r['confidence']:.2f}")
            print(f"{'':13s}{str(r.get('explanation'))[:76]}")
        else:
            print(f"{flag} {dt:6.2f}s  {query:30s} -> REFUSED: {str(r.get('reason'))[:44]}")

    print(f"\navg {total / len(QUERIES):.2f}s over {len(QUERIES)} queries")

    if args.http:
        # Re-ask one query. Over HTTP this must come back from the answer cache
        # in single-digit milliseconds. If it does not, the cache is not working
        # -- which matters a lot on Groq's free tier (see the note in api.py).
        t = time.perf_counter()
        ask(QUERIES[0][0])
        dt = time.perf_counter() - t
        verdict = "cache HIT" if dt < 0.05 else "cache MISS -- expected a hit here"
        print(f"repeat query: {dt * 1000:.1f}ms  ({verdict})")

    if surprises:
        print(f"\n{len(surprises)} result(s) differed from expectation: {surprises}")
    else:
        print("\nall results matched expectations")


if __name__ == "__main__":
    main()
