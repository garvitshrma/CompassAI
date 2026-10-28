"""
===============================================================================
 search.py — the original proof of concept (superseded by retrieval.py)
===============================================================================

Step 3: Embed chunks and search them.

Proves the core retrieval works BEFORE any LLM or widget exists.
Type a question -> get back the page URL and the CSS selector to
scroll to. That pair is the whole product.

First run downloads a ~90MB model, then it's offline and free.

-------------------------------------------------------------------------------
 WHY THIS FILE STILL EXISTS
-------------------------------------------------------------------------------
retrieval.py replaced it (hybrid scoring, multi-site, fastembed/ONNX). But this
file is kept deliberately, for two reasons:

 1. IT IS THE HONEST FIRST EXPERIMENT. Before building an API, a widget, an LLM
    layer and a deployment, the question was: "does semantic search over
    heading-chunks actually retrieve the right section?" This 90-line script
    answered that in an afternoon. If the answer had been no, none of the rest
    would have been worth writing.

    That sequencing is the real lesson here — build the smallest thing that can
    falsify your core assumption, before you build anything around it.

 2. IT IS THE SIMPLEST POSSIBLE READING OF THE SEARCH IDEA. retrieval.py has
    hybrid weights, filler stripping, level bonuses and per-site loading layered
    on. This file has none of that: embed, dot product, sort. If you want to
    understand the mechanism, read this file first, then read retrieval.py to
    see what production hardening looks like.

 DIFFERENCES FROM retrieval.py, IN ONE TABLE
 -------------------------------------------
    this file                      retrieval.py
    ---------------------------    -------------------------------------------
    sentence-transformers (torch)  fastembed (ONNX) — fits in 512MB
    semantic only                  hybrid: semantic + fuzzy lexical + rank bonus
    one global chunks.json         one index per site under sites/<id>/
    interactive CLI loop           a stateless function api.py calls
    raw query                      filler words stripped for the lexical half

    read more:
      SentenceTransformers .. https://www.sbert.net/
      Cosine similarity ..... https://en.wikipedia.org/wiki/Cosine_similarity
      Vector search intro ... https://www.pinecone.io/learn/vector-similarity/

Usage:
    pip install sentence-transformers numpy
    python search.py

Input:   chunks.json  (from chunker.py)
Cache:   embeddings.npy
"""

import json
from pathlib import Path

import os
# ---- an OpenMP workaround, not something you would normally write -----------
# PyTorch and NumPy each bundle their own copy of the Intel OpenMP runtime
# (libiomp5). On Windows, loading two copies into one process makes the runtime
# abort with "OMP: Error #15 ... multiple copies of the OpenMP runtime". This
# env var tells it to tolerate that instead of crashing.
#
# It MUST be set before the offending library is imported, which is why this
# line sits above the sentence_transformers import rather than with the rest of
# the imports. It is a known band-aid, safe here, and one of the practical
# reasons the project later moved to ONNX/fastembed — which has no OpenMP
# conflict at all.
# read more: https://github.com/dmlc/xgboost/issues/1715
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# (json and Path are imported twice — harmless leftover from when the env var
#  line was inserted between two import blocks. Python caches modules, so the
#  second import is a no-op.)
import json
from pathlib import Path

import numpy as np

# The original, PyTorch-backed embedding library. Excellent and the standard
# choice — but PyTorch alone is ~800MB installed, which is what forced the
# switch to fastembed for the deployed server. Same model weights either way.
from sentence_transformers import SentenceTransformer

CHUNKS = Path("chunks.json")
CACHE = Path("embeddings.npy")

# The short name resolves to sentence-transformers/all-MiniLM-L6-v2 on Hugging
# Face and is downloaded automatically on first use (~90MB), then cached in
# ~/.cache/huggingface. After that it runs entirely offline and free — no API
# key, no per-query cost, no data leaving the machine.
# read more: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
MODEL = "all-MiniLM-L6-v2"
TOP_K = 3


def build_text(c):
    """
    What actually gets embedded.

    Heading is repeated and page title prepended so that short,
    label-like headings ("Campus-Marketplace") still carry weight
    against longer body text.

    WHY REPETITION WORKS: an embedding is roughly a weighted blend of the
    meaning of its input. A 1500-character body would otherwise drown out a
    20-character heading. Repeating the heading doubles its share of the blend.

    That is what we want, because the heading is the most NAVIGATIONALLY useful
    part — a visitor asking "where is the marketplace" cares about the section
    titled "Campus Marketplace", not about which words happen to appear in its
    description.

    (retrieval.py extends this same idea by also prepending words derived from
     the URL slug — see slug_words() there.)
    """
    return f"{c['page_title']} | {c['heading']} | {c['heading']} | {c['content']}"


def load():
    """Load chunks + model, and either reuse or rebuild the embedding cache."""
    chunks = json.loads(CHUNKS.read_text(encoding="utf-8"))
    model = SentenceTransformer(MODEL)

    # ---- the cache, and its validity check --------------------------------
    # Embedding a few hundred chunks takes seconds; loading a .npy file takes
    # milliseconds. So we cache. But a stale cache is WORSE than no cache: the
    # search code assumes row i of the matrix corresponds to chunks[i], so an
    # out-of-date matrix returns confidently wrong results with no error at all.
    #
    # `len(vecs) == len(chunks)` is a cheap heuristic that catches essentially
    # every real change (adding, removing, or re-crawling pages all change the
    # count). It would miss an edit that changed text while keeping the count
    # identical — which is precisely why the production path in api.py adds a
    # SHA-256 hash of the page HTML on top.
    if CACHE.exists():
        vecs = np.load(CACHE)
        if len(vecs) == len(chunks):
            print(f"loaded {len(chunks)} cached embeddings")
            return chunks, model, vecs
        print("chunk count changed, re-embedding...")

    print(f"embedding {len(chunks)} chunks...")
    vecs = model.encode(
        # The list comprehension preserves chunks.json's order, which is what
        # keeps "row i == chunks[i]" true.
        [build_text(c) for c in chunks],

        # THE KEY FLAG. Scaling every vector to length 1 means the cosine
        # similarity formula  (a.b)/(|a||b|)  loses its denominator and collapses
        # to a plain dot product. We pay this cost once, here, and every future
        # query becomes a single matrix multiply.
        normalize_embeddings=True,      # so dot product == cosine similarity

        # A tqdm progress bar. Trivial, but genuinely useful: embedding a few
        # hundred chunks takes long enough that silence looks like a hang.
        show_progress_bar=True,
    )
    np.save(CACHE, vecs)
    return chunks, model, vecs


def search(query, chunks, model, vecs, k=TOP_K):
    """
    The entire search engine, in four lines.

    THIS IS THE CORE MECHANISM OF THE WHOLE PROJECT, so it is worth stating
    plainly what happens:

      1. The query becomes a 384-number vector describing its meaning.
      2. `vecs @ q` computes the dot product between that vector and EVERY
         chunk vector at once — shapes (N, 384) @ (384,) -> (N,). Because
         everything is unit length, each result IS the cosine similarity, a
         number in [-1, 1] where higher means "more similar in meaning".
      3. argsort on the negated array gives the indices of the best k. (NumPy
         has no descending sort, so negating is the standard idiom.)
      4. Return those chunks with their scores.

    No database. No index structure. No network call. For a few hundred chunks
    this is microseconds of optimised C, which is exactly why the production
    version can skip a vector database entirely.

    read more (the @ operator): https://peps.python.org/pep-0465/
    """
    q = model.encode([query], normalize_embeddings=True)[0]
    scores = vecs @ q                    # cosine similarity, all at once
    top = np.argsort(-scores)[:k]
    # float(...) converts np.float32 to a plain Python float so it prints and
    # serialises cleanly.
    return [(chunks[i], float(scores[i])) for i in top]


def main():
    """A REPL for eyeballing retrieval quality.

    Reading raw scores next to real queries is how the weights, the filler list
    and the confidence floor in the production files were all tuned. There is no
    substitute for typing twenty real questions and looking at what comes back.
    """
    chunks, model, vecs = load()
    print("\nAsk something (blank line to quit).")
    print("try: where can I find the marketplace project\n")

    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            # Catching both means Ctrl-C and Ctrl-D (or a piped-in file ending)
            # exit cleanly instead of dumping a traceback. A small courtesy that
            # makes a CLI feel finished.
            break
        if not q:                        # a blank line also quits
            break

        for c, score in search(q, chunks, model, vecs):
            # Printing the SELECTOR alongside the URL is the point of this
            # script: that (url, selector) pair is what proves navigation is
            # possible, long before any widget exists to act on it.
            print(f"\n  [{score:.3f}] {c['heading']}  ({c['level']})")
            print(f"      url:      {c['url']}")
            print(f"      selector: {c['selector']}")
            print(f"      content:  {c['content'][:110]}...")
        print()


if __name__ == "__main__":
    main()
