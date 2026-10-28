"""
===============================================================================
 navigator/state.py — the server's in-memory state, lifted out of api.py.
===============================================================================

Everything in this file is COPIED, not redesigned. It is pure Python with no
FastAPI in it, so there was nothing to port — which is itself the lesson of this
whole exercise: the framework swap only touches code that talks HTTP.

It lives in its own module rather than in views.py for one practical reason.
Under FastAPI, STATE and the cache sat in the same file as the endpoints, which
meant you could not import the cache from anywhere else without also importing
the web app. Now `from navigator.state import STATE` works from a management
command or a test with no HTTP machinery involved.

  STATE["sites"] = { "openlake.in": (chunks_list, vectors_matrix), ... }
  STATE["llm"]   = the shared Groq client

THE TRADE-OFF, RESTATED HONESTLY
--------------------------------
This state lives in ONE process's memory, and it works only because we run a
single worker. Two workers would each hold their own divergent copy: a site
indexed by worker A would appear unregistered to worker B, and the answer cache
would be split in half. This is the exact constraint that makes "move to
Postgres + pgvector" step 2 rather than a nice-to-have, and it is why
render.yaml pins --workers 1.
"""

import logging
import threading
from collections import OrderedDict

log = logging.getLogger("compass")

STATE = {"sites": {}, "llm": None}


# ---- the answer cache -------------------------------------------------------
# WHY A CACHE EARNS ITS PLACE HERE, when caches are usually premature:
#
# A navigation widget sees the SAME handful of questions over and over. Every
# visitor to a gym site asks about the coaches; every student asks about fees.
# The query distribution is tiny and extremely repetitive — exactly the shape
# where a cache pays off.
#
# It fixes both of this project's real problems at once:
#   LATENCY — a hit answers in microseconds instead of ~600ms.
#   RATE LIMITS — Groq's free tier allows 8,000 tokens per minute and one query
#     costs ~700, so the WHOLE SERVER can serve roughly ten uncached queries a
#     minute across every customer before it starts returning 429s. Cached
#     answers cost zero tokens.
#
# CORRECTNESS: the key includes site_id, so no site can read another's answer.
# It is invalidated on re-index, because a fresh crawl can change which selector
# a question should resolve to.
_CACHE_MAX = 500
_cache = OrderedDict()
_cache_lock = threading.Lock()


def cache_key(sid, query):
    """Normalise so trivial variations share one entry.

    "Where are the Projects?" / "where are the projects" / "  WHERE ARE THE
    PROJECTS " all collapse to the same key. `.split()` with no argument splits
    on arbitrary runs of whitespace and drops empties, so re-joining with single
    spaces normalises internal spacing too.
    """
    return sid, " ".join(query.lower().split())


def cache_get(sid, query):
    with _cache_lock:
        key = cache_key(sid, query)
        if key in _cache:
            # Mark as most-recently-used, so hot queries survive eviction.
            _cache.move_to_end(key)
            return _cache[key]
    return None


def cache_put(sid, query, value):
    with _cache_lock:
        _cache[cache_key(sid, query)] = value
        # last=False pops from the FRONT (least recently used).
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def invalidate_cache(sid):
    """Drop every cached answer for one site, after its index changes.

    The doomed key list is built BEFORE deleting: mutating a dict while
    iterating it raises RuntimeError.
    """
    with _cache_lock:
        for key in [k for k in _cache if k[0] == sid]:
            del _cache[key]


# ---- concurrency control ----------------------------------------------------
# THE PROBLEM: /auto-register kicks off a crawl that can take 30+ seconds. If
# ten visitors land on an unindexed site at the same moment, ten crawls start in
# parallel — ten times the bandwidth hitting the customer's server, ten times
# our memory, and ten racing writes to the same chunks.json.
#
# THE FIX: one lock PER SITE. Different sites index simultaneously; the same
# site cannot.
#
# `_lock_guard` protects the DICTIONARY OF LOCKS. Without it, two threads could
# both find `sid not in _indexing_locks`, both create a NEW Lock, and each take
# their own — meaning neither is actually excluded.
#
# THIS STILL WORKS THE SAME WAY UNDER DJANGO, and it is worth knowing why:
# gunicorn is configured with one worker and several THREADS (not processes), so
# all requests share this module's globals exactly as they did under uvicorn.
# Switch to multiple workers and these locks silently stop protecting anything,
# because each process gets its own copy.
_indexing_locks = {}
_lock_guard = threading.Lock()


def get_site_lock(sid):
    """Get-or-create this site's lock.

    `dict.setdefault(key, default)` returns the existing value if present,
    otherwise inserts the default and returns it — an atomic get-or-create.
    """
    with _lock_guard:
        return _indexing_locks.setdefault(sid, threading.Lock())


def drop_site_lock(sid):
    """Remove a lock object so the dict does not grow one entry per site seen
    forever (a slow leak on a server that meets many domains)."""
    with _lock_guard:
        _indexing_locks.pop(sid, None)
