"""
===============================================================================
 retrieval.py  —  STAGE 3 of the Compass pipeline (the search engine)
===============================================================================

Hybrid retrieval: semantic (fastembed / ONNX) + lexical (rapidfuzz).
Multi-site aware: each registered site has its own index under sites/<site_id>/.

Layout:
    sites/
      openlake.in/
        chunks.json
        embeddings.npy
      example.com/
        chunks.json
        embeddings.npy

The embedding model is shared across all sites (embeddings are model-specific
but site-independent). Only the chunks + vectors differ per site.

-------------------------------------------------------------------------------
 THE BIG IDEA: WHY "HYBRID" SEARCH?
-------------------------------------------------------------------------------
There are two completely different ways to decide whether a query matches a
piece of text, and each one fails in exactly the place the other succeeds.

1. SEMANTIC (dense / vector search)
   Turn text into a list of 384 numbers ("an embedding") using a neural network
   trained so that text with similar MEANING lands near each other in that
   384-dimensional space. Then "similar" is just geometric closeness.
     WINS AT: synonyms and paraphrase. The query "codeforces" retrieves a
              project called "canonforces"; "how much does it cost" finds a
              section titled "Fees".
     FAILS AT: exact identifiers and typos. Rare proper nouns were barely in the
              model's training data, so their vectors are close to meaningless.

2. LEXICAL (sparse / keyword / fuzzy string search)
   Compare the literal characters. rapidfuzz measures how many edits turn one
   string into another.
     WINS AT: exact page names and typos. "phylosophy" still matches
              "Philosophy" because only one character differs.
     FAILS AT: synonyms. "cost" and "fees" share almost no letters, so lexical
              scoring says they are unrelated.

Neither is good enough alone, so we compute BOTH and blend them with weights.
This is standard practice in modern production search.

    read more:
      Hybrid search explained ...... https://www.pinecone.io/learn/hybrid-search-intro/
      Sentence embeddings .......... https://www.sbert.net/
      Cosine similarity ............ https://en.wikipedia.org/wiki/Cosine_similarity
      Levenshtein / edit distance .. https://en.wikipedia.org/wiki/Levenshtein_distance

Usage from api.py:
    from retrieval import load_all_sites, load_site, search, model
"""

import json
import re
from pathlib import Path
from urllib.parse import urlparse

# NumPy gives us fast array maths implemented in C. The single most important
# thing it buys us here: we can score ALL chunks at once with one matrix
# multiply instead of looping in Python, which is roughly a 100x speedup.
# read more: https://numpy.org/doc/stable/user/absolute_basics.html
import numpy as np

# rapidfuzz is a fast C++ implementation of fuzzy string matching (a much
# faster successor to the older `fuzzywuzzy`).
# read more: https://rapidfuzz.github.io/RapidFuzz/
from rapidfuzz import fuzz

# fastembed runs sentence-transformer models through ONNX Runtime instead of
# PyTorch. See EMBED_MODEL below for why that choice is load-bearing.
# read more: https://qdrant.github.io/fastembed/
from fastembed import TextEmbedding

SITES_DIR = Path("sites")

# THE EMBEDDING MODEL — and why this exact one.
#   all-MiniLM-L6-v2 is the standard "small and good enough" sentence embedding
#   model: 6 transformer layers, ~22M parameters, ~90MB on disk, 384 dimensions
#   per vector. Bigger models (768 or 1024 dims) score a few percent better on
#   benchmarks but are several times larger and slower.
#
#   CRITICAL DEPLOYMENT DETAIL: we load it through fastembed (ONNX Runtime), not
#   through sentence-transformers. sentence-transformers depends on PyTorch,
#   which alone is roughly 800MB installed. Render's free tier gives us 512MB
#   total. Same model, same weights, same output vectors — different runtime,
#   and that is the only reason this project can be deployed for free.
#   read more: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"   # 384-dim, ONNX

# ---- the blend weights ------------------------------------------------------
# These sum to 1.0, so the combined score stays roughly on a 0..1 scale, which
# is what makes the CONFIDENCE_FLOOR in answer.py a meaningful, tunable number.
# Semantic gets the larger share because most real visitor questions are
# phrased conversationally ("where do I sign up") rather than as exact page
# names; lexical is the corrective minority vote.
# These were tuned by hand against real queries on openlake.in.
W_SEMANTIC = 0.65
W_LEXICAL = 0.35

# ---- the content-IDF signal --------------------------------------------------
# A third retrieval signal: verbatim query tokens found inside chunk CONTENT,
# weighted by token rarity (a miniature IDF over the site's own corpus).
#
# WHY IT EXISTS — this is the fix for the Readme's known issue "individual
# person names on team pages aren't retrievable yet". The lexical half above
# deliberately ignores content (fuzzy-matching long bodies is noise), but a
# person's name lives ONLY in body text. Semantic embeddings barely know rare
# personal names either, so such queries used to score ~0.25 and die at
# answer.py's confidence floor even when the person was plainly on the site.
#
# THE IDF TWIST is what keeps it from wrecking everything else: a surname
# appearing in 1 of 136 chunks gets weight ~0.78 (rare = informative), while
# "projects" — present in half the site — gets ~0.24, and words present in
# every chunk or in none get exactly zero. Common navigation words therefore
# cannot buy their way past the floor; genuinely distinctive tokens can.
#
# Weight 0.40 rather than a smaller nudge: for a single-token name query the
# semantic half contributes little (~0.13), so the content signal must be
# strong enough to carry a correct match over CONFIDENCE_FLOOR on its own.
# False positives that sneak past are still caught by GATE 2 (the LLM), and
# cost only one cached-then-done API call.
import math

W_CONTENT = 0.40

# ---- the heading-rank bonus -------------------------------------------------
# A small additive nudge based on how important a heading is in the document.
#
# WHY: consider the query "take me to the projects page". The h1 "Projects" at
# the top of /projects and the h3 "Projects" buried in an unrelated page's
# sidebar may score almost identically on text alone. But an h1 IS the page's
# identity — landing there gives the visitor the whole page, and they can scroll
# from there. Landing on a deep h3 strands them mid-document.
#
# The values are small on purpose (0.08 out of a ~1.0 scale). This is a
# tie-breaker that decides near-draws, not a force strong enough to override a
# genuinely better text match.
LEVEL_BONUS = {"h1": 0.08, "h2": 0.0, "h3": -0.02}

# ---- stopwords, but tuned for NAVIGATION queries ----------------------------
# A "stopword list" is a set of words carrying so little meaning that they add
# noise rather than signal. Ours is unusual: alongside the normal grammar words
# it includes navigation verbs — "take", "show", "find", "page" — because
# Compass answers commands like "take me to the projects page", not questions.
# After stripping, that query becomes just "projects", which is exactly the
# string we want to fuzzy-match against heading text.
#
# A `set` is used rather than a list so that `w not in FILLER` is an O(1) hash
# lookup instead of an O(n) scan.
# read more: https://en.wikipedia.org/wiki/Stop_word
FILLER = {"where", "is", "are", "the", "all", "of", "a", "an", "to",
          "me", "take", "show", "find", "i", "want", "can", "how",
          "do", "on", "in", "at", "page", "please", "what"}

# Module-level cache for the loaded model. The leading underscore is a Python
# convention meaning "private, do not touch from outside this module".
# read more: https://peps.python.org/pep-0008/#descriptive-naming-styles
_model = None


def model():
    """Load the ONNX embedder once, lazily, shared across all sites.

    THIS IS THE SINGLETON / LAZY-INITIALISATION PATTERN.

    Loading the model means reading ~90MB from disk and initialising an ONNX
    session — around a second of work, and a permanent chunk of RAM. We must do
    it exactly once for the entire process lifetime:

      * NOT at import time, because then merely importing this module (e.g. in a
        test, or in register_site.py which never embeds anything) would pay the
        cost for nothing.
      * NOT per request, which would make every query take a second and would
        thrash memory on a 512MB box.

    `global _model` is required because without it, the assignment `_model = ...`
    would create a NEW variable local to this function and the cache would never
    persist. `global` tells Python we mean the module-level one.
    read more: https://docs.python.org/3/reference/simple_stmts.html#the-global-statement

    Note that api.py deliberately CALLS this during startup (in `lifespan`) to
    "warm" the model, so that the very first visitor is not the one who pays the
    one-second load.
    """
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=EMBED_MODEL)
    return _model


def _encode(texts):
    """Turn a list of strings into a matrix of unit-length vectors.

    Returns shape (len(texts), 384) — one row per input string.

    STEP 1 — embed.
      `model().embed(texts)` returns a GENERATOR (lazy, yields one vector at a
      time to save memory). `list(...)` forces it to actually run, then
      np.array stacks the vectors into a 2-D matrix.
      dtype=np.float32 halves memory versus the float64 NumPy defaults to.
      For 384-dim vectors the precision difference is irrelevant to ranking,
      and on a 512MB server the memory is not.

    STEP 2 — normalise to unit length. THIS IS THE IMPORTANT PART.
      What we actually want to measure is COSINE SIMILARITY:

            cos(a, b) = (a . b) / (|a| * |b|)

      That division is expensive to do for every chunk on every query. But if we
      pre-scale every vector so that |a| = 1 and |b| = 1, the denominator becomes
      1 and the formula collapses to:

            cos(a, b) = a . b        (just a dot product)

      Which means the entire search becomes ONE matrix multiply: `vecs @ q`.
      We pay the normalisation cost once at index time and get free, exact
      cosine similarity on every query forever after.
      read more: https://en.wikipedia.org/wiki/Cosine_similarity
    """
    vecs = np.array(list(model().embed(texts)), dtype=np.float32)

    # np.linalg.norm computes each vector's length (its L2 / Euclidean norm).
    #   axis=1        -> compute one norm per ROW (per vector), not for the
    #                    whole matrix
    #   keepdims=True -> keep the result shaped (N, 1) instead of (N,), so that
    #                    NumPy "broadcasting" can divide the (N, 384) matrix by
    #                    it row-wise. Without keepdims the shapes would not line
    #                    up and you would get an error or, worse, wrong maths.
    # read more (broadcasting): https://numpy.org/doc/stable/user/basics.broadcasting.html
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)

    # np.clip(norms, 1e-8, None) raises anything below 1e-8 up to 1e-8, leaving
    # the upper bound untouched (None = no maximum). This is a divide-by-zero
    # guard: a degenerate all-zeros vector would otherwise produce NaN, and a
    # single NaN silently corrupts every comparison it touches (NaN compares
    # False against everything, so argsort results become nonsense).
    return vecs / np.clip(norms, 1e-8, None)


def embed_query(query):
    """Embed one query string and return it as a flat 384-length vector.

    `_encode` always returns a 2-D matrix, so for a single string we get shape
    (1, 384). The `[0]` unwraps that outer dimension to a plain (384,) vector,
    which is the shape the `vecs @ q` matrix-vector product expects.
    """
    return _encode([query])[0]


# ---------- text builders (shared) ----------
# These three helpers decide WHAT TEXT gets compared. They matter enormously —
# a retrieval system's quality is determined at least as much by what you feed
# it as by the model you feed it to.

def strip_filler(query):
    """Reduce a navigation command to its content words.

        "take me to the projects page"  ->  "projects"
        "where are all of the fees"     ->  "fees"

    WHY THIS ONLY AFFECTS THE LEXICAL HALF (see `search` below):
    Fuzzy string similarity is computed over the WHOLE string, so filler words
    dilute it. Matching "take me to the projects page" against the heading
    "Projects" scores poorly simply because five of the six words are noise.
    Matching "projects" against "Projects" scores ~100.

    The embedding model, by contrast, was trained on natural sentences and
    handles filler words perfectly well on its own — so we deliberately pass it
    the ORIGINAL untouched query.

    THE `or query.lower()` FALLBACK:
    If someone types a query made entirely of filler ("where is it"), the list
    comprehension yields nothing and " ".join([]) is the empty string. Fuzzy
    matching an empty string against everything returns garbage, so we fall back
    to the original text. An empty string is falsy in Python, which is what makes
    this one-liner work.
    """
    words = [w for w in query.lower().split() if w not in FILLER]
    return " ".join(words) or query.lower()


def slug_words(url):
    """Turn a URL path into searchable English.

        https://site.com/team-members   ->  "team members"
        https://site.com/about_us/staff ->  "about us staff"
        https://site.com/               ->  "home homepage main index"

    WHY THIS IS WORTH DOING:
    A site's URL structure is human-authored metadata that we would otherwise
    throw away. If a page lives at /admissions, the word "admissions" is a
    strong signal about that page even if the visible heading says something
    vaguer like "Join Us". Feeding the slug into both the embedded text and the
    fuzzy-match target recovers that signal for free.

    THE HOMEPAGE SPECIAL CASE:
    A homepage's path is empty, so it would contribute nothing. But visitors
    absolutely do say "take me home" / "go to the main page". We therefore
    inject four synonyms so those queries have something to match against.
    """
    path = urlparse(url).path.strip("/")
    if not path:
        return "home homepage main index"
    return path.replace("-", " ").replace("_", " ").replace("/", " ")


def build_text(c):
    """The text that actually gets EMBEDDED for a chunk.

    Note the deliberate structure — four fields, pipe-separated, with the
    heading appearing TWICE:

        <url slug> | <page title> | <heading> | <heading> | <content>

    WHY REPEAT THE HEADING?
    An embedding is, loosely, a weighted average of the meaning of everything in
    the input. So a 1500-character body will dominate a 20-character heading and
    the heading's meaning gets washed out. Repeating it doubles its influence.

    That is exactly what we want, because the heading is the most
    NAVIGATIONALLY relevant part: a visitor asking "where is the marketplace"
    cares about the section titled "Campus Marketplace", not about which
    incidental words appear in its description.

    This is a cheap, well-known trick sometimes called field weighting or field
    boosting — the same idea BM25 implements with per-field weights.
    read more: https://en.wikipedia.org/wiki/Okapi_BM25
    """
    return (f"{slug_words(c['url'])} | {c['page_title']} | "
            f"{c['heading']} | {c['heading']} | {c['content']}")


def lexical_target(c):
    """The text that fuzzy string matching runs against.

    NOTICE WHAT IS MISSING: c['content'].

    That omission is deliberate and important. `fuzz.token_set_ratio` compares
    sets of words, so a long body text contains so many words that it will
    partially match almost ANY query by coincidence. Including content would
    make every long chunk score high on every query — the lexical signal would
    become noise and actively damage the blend.

    So the two halves of hybrid search deliberately look at DIFFERENT text:
      semantic -> slug + title + heading + heading + content   (rich, nuanced)
      lexical  -> slug + title + heading                       (short, precise)
    """
    return f"{slug_words(c['url'])} {c['page_title']} {c['heading']}"


# ---------- per-site loading ----------
# Compass is MULTI-TENANT: one running server holds a separate index for every
# registered website. The isolation is done with plain folders on disk, one per
# site, which is about as simple as multi-tenancy can be.

def site_dir(site_id):
    """Path to one site's folder. `/` on Path objects is the join operator, and
    it produces the correct separator for the current OS automatically."""
    return SITES_DIR / site_id


def embed_site(site_id):
    """(Re)build embeddings.npy for one site from its chunks.json.

    This is the expensive operation in the whole system — every chunk has to go
    through the neural network. For a few hundred chunks it takes seconds, which
    is why we cache the result to disk rather than recomputing at startup.

    Called whenever the chunk set CHANGES: after /register, /auto-register, or
    /ingest in api.py.
    """
    d = site_dir(site_id)
    chunks = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
    print(f"[{site_id}] embedding {len(chunks)} chunks...")

    # The list comprehension preserves chunks.json's order, which preserves the
    # invariant "row i of the matrix IS chunks[i]". Everything downstream
    # depends on that.
    vecs = _encode([build_text(c) for c in chunks])

    # .npy is NumPy's own binary format. Compared to saving as JSON it is far
    # smaller (raw float32 bytes, no text encoding) and loads near-instantly
    # via memory mapping rather than being parsed.
    # read more: https://numpy.org/doc/stable/reference/generated/numpy.save.html
    np.save(d / "embeddings.npy", vecs)
    return chunks, vecs


def load_site(site_id, reembed=False):
    """Load one site's chunks + vectors. Embeds if missing or reembed=True.

    THE CACHE-VALIDITY CHECK IS THE INTERESTING LINE HERE.

    We must never use a stale embeddings.npy, because the search code assumes
    row i of the matrix corresponds to chunks[i]. If someone edits chunks.json
    and we load an old matrix, every result silently points at the WRONG text —
    a bug that produces no error, just quietly wrong answers.

    `len(vecs) == len(chunks)` is a cheap, imperfect but very effective guard:
    almost any real change to a site (a page added, removed, or re-crawled)
    changes the chunk count. It would miss an edit that changes text while
    keeping the count identical, which is why /ingest in api.py additionally
    tracks a SHA-256 hash of each page's HTML.
    """
    d = site_dir(site_id)
    chunks = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
    cache = d / "embeddings.npy"
    if cache.exists() and not reembed:
        vecs = np.load(cache)
        if len(vecs) == len(chunks):
            return chunks, vecs
    return embed_site(site_id)


def load_all_sites():
    """Load every site under sites/ into {site_id: (chunks, vecs)}.
    Called once at API startup.

    The returned dict IS the server's whole in-memory database:

        {"openlake.in": (chunks_list, vectors_matrix), "gdg.dev": (...), ...}

    Doing this once at boot means individual requests never touch the disk or
    the embedder — a query is pure in-memory maths, which is why it is fast.
    """
    registry = {}

    # A brand-new deployment has no sites/ folder yet. Returning an empty
    # registry lets the server boot successfully and accept /auto-register
    # calls, rather than crashing on startup.
    if not SITES_DIR.exists():
        return registry

    # `sorted()` gives deterministic load order, which makes startup logs
    # comparable between runs — small thing, genuinely useful when debugging.
    for d in sorted(SITES_DIR.iterdir()):
        # Two guards: it must be a directory (ignore stray files like .DS_Store
        # or .gitkeep), and it must actually contain chunks.json (ignore a
        # half-written folder from a crash mid-write).
        if d.is_dir() and (d / "chunks.json").exists():
            try:
                registry[d.name] = load_site(d.name)
                print(f"loaded site '{d.name}' ({len(registry[d.name][0])} chunks)")
            except Exception as e:
                # DELIBERATE FAULT ISOLATION: one site with corrupt JSON must
                # not prevent the server from starting and serving the other
                # nine. We log and skip. In a multi-tenant system, blast-radius
                # containment like this is the difference between one broken
                # customer and total downtime.
                print(f"skip site '{d.name}': {e}")
    return registry


# ---------- search (per-site) ----------

def search(query, chunks, vecs, k=3, debug=False):
    """
    Score every chunk against the query and return the top k.

    Note this function is STATELESS — chunks and vecs are passed IN rather than
    read from a global. That is what makes the server multi-tenant: api.py hands
    in whichever site's data the request is for, and the same code serves all of
    them with no shared state and therefore no cross-site leakage.

    Returns: a list of (chunk_dict, score) tuples, best first.
    """
    # ---- SIGNAL 1: semantic similarity -----------------------------------
    q = embed_query(query)

    # `@` is Python's matrix-multiplication operator (added in Python 3.5).
    # Shapes: (N, 384) @ (384,) -> (N,)
    # i.e. one similarity score per chunk, computed in a single optimised C/BLAS
    # call rather than an N-iteration Python loop. Because both sides were
    # normalised to unit length in _encode, each result IS the cosine similarity,
    # bounded in [-1, 1] and in practice roughly [0, 1] for real text.
    # read more: https://peps.python.org/pep-0465/
    sem = vecs @ q

    # ---- SIGNAL 2: fuzzy lexical similarity ------------------------------
    lean = strip_filler(query)

    # WHY token_set_ratio SPECIFICALLY?
    # rapidfuzz offers several scorers:
    #   ratio             - plain edit distance over the whole string; punishes
    #                       any difference in length or word order
    #   partial_ratio     - best matching substring
    #   token_sort_ratio  - sorts the words first, so order stops mattering
    #   token_set_ratio   - treats each side as a SET of words and compares the
    #                       common words against the leftovers
    #
    # token_set_ratio is the most forgiving, and forgiveness is what we need:
    # the query "projects" should score near-perfectly against the target
    # "projects openlake projects" even though the target has extra words. Word
    # order in a heading is arbitrary, and query length never matches target
    # length. It also still catches typos, since the underlying comparison is
    # still edit-distance based.
    # read more: https://rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html
    #
    # rapidfuzz returns 0..100, so we divide by 100 to put it on the same 0..1
    # scale as the cosine score. Both signals MUST share a scale before you can
    # meaningfully add them with weights — otherwise the weights are lying.
    lex = np.array([
        fuzz.token_set_ratio(lean, lexical_target(c).lower()) / 100.0
        for c in chunks
    ])

    # ---- SIGNAL 3: rare-token hits inside CONTENT (see W_CONTENT above) ---
    # Tokenise the filler-stripped query, then for each DISTINCT token compute
    # its document frequency across this site's chunk contents. The idf-style
    # weight log((N+1)/(df+1)) / log(N+1) lands in (0, 1]: near 1 for tokens
    # almost nowhere on the site, near 0 for tokens everywhere. A token found
    # in NO chunk gets no weight either — absence of evidence is not evidence.
    #
    # NORMALISATION MATTERS: we take the MEAN over tokens that exist somewhere
    # on the site (df > 0), not the sum. Summing let a long query whose words
    # all appear in some rich blog post rack up cnt > 1.4 and steamroll the
    # ranking — a real bug that sent "game development resources" to a blog
    # post instead of the closest genuine destination. With the mean, the
    # signal's maximum contribution stays W_CONTENT x ~1.0 no matter how many
    # tokens the query has. Tokens that appear NOWHERE are excluded from the
    # denominator so an unknown word doesn't dilute a well-covered query.
    #
    # Cost: one lowercase pass over every content string per distinct query
    # token. At a few hundred chunks that is well under a millisecond of
    # Python; if sites ever grow to tens of thousands of chunks, precompute
    # lowered contents once at load time instead.
    q_tokens = set(re.findall(r"[a-z0-9]{3,}", lean))
    cnt = np.zeros(len(chunks), dtype=np.float32)
    if q_tokens and chunks:
        n = len(chunks)
        idf_denom = math.log(n + 1)
        lowered = [c["content"].lower() for c in chunks]
        present = np.zeros(n, dtype=np.float32)
        matched = 0
        for tok in q_tokens:
            df = sum(1 for txt in lowered if tok in txt)
            if df == 0 or df == n:      # unknown word / everywhere-word: no signal
                continue
            w = math.log((n + 1) / (df + 1)) / idf_denom
            present += np.fromiter((w if tok in txt else 0.0 for txt in lowered),
                                   dtype=np.float32, count=n)
            matched += 1
        if matched:
            cnt = present / matched

    # ---- SIGNAL 4: the structural rank bonus -----------------------------
    # `.get(key, 0.0)` rather than `[key]` so that an unexpected level value
    # (e.g. the "p" used by indexer.py's text-window fallback chunks) yields a
    # neutral 0.0 instead of raising KeyError.
    bonus = np.array([LEVEL_BONUS.get(c["level"], 0.0) for c in chunks])

    # ---- the blend -------------------------------------------------------
    # All four are NumPy arrays of length N, so this single line does N
    # multiply-adds elementwise in C. No loop.
    combined = W_SEMANTIC * sem + W_LEXICAL * lex + W_CONTENT * cnt + bonus

    # ---- pick the winners ------------------------------------------------
    # np.argsort returns the INDICES that would sort the array ascending. NumPy
    # has no "descending" flag, so the standard idiom is to sort the NEGATED
    # array — largest value becomes most-negative and therefore sorts first.
    # `[:k]` then takes the top k indices.
    #
    # (For very large N you would use np.argpartition, which is O(n) instead of
    #  O(n log n) because it only partially sorts. At a few hundred chunks per
    #  site the difference is unmeasurable, so we keep the clearer version.)
    # read more: https://numpy.org/doc/stable/reference/generated/numpy.argsort.html
    top = np.argsort(-combined)[:k]

    out = []
    for i in top:
        # float(...) converts NumPy's np.float32 into a plain Python float.
        # This matters at the boundary of the system: np.float32 is not JSON
        # serialisable, and FastAPI would raise when trying to send the response.
        out.append((chunks[i], float(combined[i])))

        # The debug branch prints the individual signals side by side. This is
        # the single most useful tool for tuning the weights: when a query
        # returns something wrong, this shows you immediately WHICH signal
        # misfired and therefore which knob to turn.
        if debug:
            print(f"    [{combined[i]:.3f}] sem={sem[i]:.3f} lex={lex[i]:.3f} "
                  f"cnt={cnt[i]:.2f}  "
                  f"{chunks[i]['heading'][:40]}  ({chunks[i]['url']})")
    return out
