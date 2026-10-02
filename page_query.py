"""
===============================================================================
 page_query.py  —  answers a question about ONE page sent by the Chrome extension
===============================================================================

WHY THIS EXISTS
---------------
The widget path (/query) only works on sites that were crawled and indexed ahead
of time. The Chrome extension has to work on ANY website, including ones Compass
has never seen. So the extension reads the page itself (in the visitor's
browser), and sends us just that page's sections together with the question.

We then run the SAME pipeline as /query on those sections, in memory:
  1. turn each section into a "chunk" with the fields retrieval.py expects
  2. embed them (cached by content, so follow-up questions on the same page
     don't re-embed)
  3. call answer() — hybrid search for candidates, then the LLM picks one

PRIVACY: nothing here is written to disk. The page's sections live only for the
length of the request, plus a small in-memory cache of their embeddings (just
numbers, keyed by a hash), which is lost on restart.
"""

import hashlib
import json
from collections import OrderedDict
from urllib.parse import urlsplit

from answer import answer
from retrieval import _encode, build_text, search

# Limits, so one request can't make the server do unbounded work.
MAX_SECTIONS = 150
MAX_HEADING_CHARS = 200
MAX_SUMMARY_CHARS = 400
MAX_QUESTION_CHARS = 300

# Below this retrieval score we don't even ask the LLM. Lower than the widget's
# 0.30 because one page's sections are short; the LLM still rejects weak matches.
PAGE_FLOOR = 0.20

# Embeddings of recently asked-about pages, newest last.
_VEC_CACHE_MAX = 64
_vec_cache: "OrderedDict[str, object]" = OrderedDict()


def clean_url(url: str) -> str:
    """Keep only scheme://host/path. Query strings and #fragments can carry
    session ids or personal data, and retrieval only needs the path words."""
    try:
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except ValueError:
        pass
    return ""


def to_chunks(url: str, title: str, sections: list) -> list:
    """Shape the extension's sections like the chunks chunker.py produces, so
    retrieval.py and answer.py can use them unchanged.

    `selector` holds the extension's own section id (e.g. "h5"). The extension
    uses it to find and scroll to the heading again.
    """
    chunks = []
    for s in sections[:MAX_SECTIONS]:
        heading = (s.get("heading") or "").strip()[:MAX_HEADING_CHARS]
        if not heading:
            continue
        chunks.append({
            "id": len(chunks),
            "url": url,
            "page_title": (title or "")[:MAX_HEADING_CHARS],
            "heading": heading,
            "level": (s.get("tag") or "h2").lower(),
            "selector": str(s.get("id") or ""),
            "content": (s.get("summary") or "").strip()[:MAX_SUMMARY_CHARS],
        })
    return chunks


def page_vectors(chunks: list):
    """Embed the chunks, reusing the result if this exact page was seen recently."""
    key = hashlib.sha256(
        json.dumps([[c["url"], c["page_title"], c["heading"], c["content"]] for c in chunks],
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if key in _vec_cache:
        _vec_cache.move_to_end(key)
        return _vec_cache[key]
    vecs = _encode([build_text(c) for c in chunks])
    _vec_cache[key] = vecs
    if len(_vec_cache) > _VEC_CACHE_MAX:
        _vec_cache.popitem(last=False)
    return vecs


def query_page(question: str, url: str, title: str, sections: list, llm) -> dict:
    """Returns the /query result shape plus:
         id       the chosen section's id (or None)
         matches  the top sections by retrieval score: [{"id", "score"}]
    """
    chunks = to_chunks(clean_url(url), title, sections)
    if not chunks:
        return {"found": False, "reason": "this page has no sections to search",
                "id": None, "matches": []}

    vecs = page_vectors(chunks)
    result = answer(question[:MAX_QUESTION_CHARS], chunks, vecs, llm, floor=PAGE_FLOOR)
    matches = [{"id": c["selector"], "score": round(float(score), 3)}
               for c, score in search(question[:MAX_QUESTION_CHARS], chunks, vecs, k=4)]

    return {**result, "id": result.get("selector") if result.get("found") else None,
            "matches": matches}
