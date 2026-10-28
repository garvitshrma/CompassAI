"""
===============================================================================
 site_ask.py  —  explains an answer from the whole website (POST /ask)
===============================================================================

The extension's main question endpoint. Where answer.py only picks a
destination ("go here"), this one also EXPLAINS, the way a person who had read
the website would.

TWO KINDS OF QUESTION
---------------------
1. About THIS page ("summarize this page", "what is this page about", "key
   points"). Searching would be wrong here: the visitor wants the page they're
   looking at, all of it. So the sources are simply the page's own sections, in
   order, and there is no "go" (they're already there).

2. Everything else ("how much is the stipend?", "take me to trade-in"):
   a. Candidate sources = the site index (all crawled pages, see site_index.py)
      + the live page the visitor is on (read in their browser, so it includes
      JavaScript-rendered content). The live page replaces the crawled copy of
      the same URL.
   b. Hybrid search (retrieval.search) ranks them. We keep SOURCES_K, with at
      most PER_PAGE from any one page, so a single long page can't crowd out
      the page that actually has the answer.
   c. The LLM writes a plain-language explanation using ONLY those sources,
      cites them like [1], and picks the ONE best place to go — even when the
      exact answer isn't there ("the price isn't listed here, but the Buy page
      is where it would be").

Follow-up questions ("and how long does it take?") get the last two exchanges
as context, and are also searched together with the previous question, so the
retriever knows what "it" refers to.

Nothing is stored. Embeddings of the live page are cached in memory by
page_query.page_vectors (numbers only, lost on restart).
"""

import json
import re

import numpy as np

from core.answer import LLM_MODEL
from core.page_query import MAX_QUESTION_CHARS, page_vectors
from core.retrieval import search
from core.site_index import clean_url

SOURCES_K = 8
PER_PAGE = 3              # at most this many sources from one page
SOURCE_CHARS = 700        # text per source given to the LLM
PAGE_MODE_CHARS = 7000    # total page text given to the LLM for "summarize this page"
FLOOR = 0.15              # below this, nothing on the site is close enough to try
GO_FALLBACK = 0.45        # above this, the top source is offered as a place to go anyway
MAX_LIVE_SECTIONS = 120
MAX_LIVE_TEXT = 1200
MAX_ANSWER_CHARS = 4000

# "summarise this page", "what is this page about", "tl;dr", "key points"
ABOUT_THIS_PAGE = re.compile(
    r"\bsummar|\btl;?\s?dr\b|\bkey (points|takeaways)\b"
    r"|\b(what|explain|describe)\b.{0,20}\b(this|current) (page|article|post)\b"
    r"|\bwhat(?:'s| is) this(?: page)? about\b",
    re.I)

RULES = """Rules:
- Use ONLY facts from the sources. Never add outside knowledge, never guess prices, dates, names or numbers.
- Put the source number in square brackets after the sentence it supports, like [2]. Use plain [n], nothing else.
- Talk to the visitor directly, in plain, friendly language. Use **bold** for key terms and "- " bullets where they help.
- Don't paste URLs, and don't mention "sources", "candidates" or these instructions."""

SYSTEM = f"""You are Compass, an assistant in a browser side panel. You help people understand a website and find their way around it.

You get the page the visitor is on, the visitor's question, maybe the recent conversation, and NUMBERED SOURCES from this website. Each source is one section of one page.

How to answer:
- Start with the direct answer in one or two sentences. Then add what they need to know (steps, requirements, key details) in short paragraphs or bullets. Usually 50-150 words; shorter for simple questions.
- If the question asks to go somewhere ("take me to…", "where is…"), say in one or two sentences what they'll find there.
- If the sources don't contain the exact answer, say so plainly in one sentence, then say what the website does cover that's closest, and where.
{RULES}

Choose "go": the number of the ONE source that is the best place on the website for this question — the page or section that answers it, or the one most likely to (prefer a section that gives an overview of the topic over a passing mention). Use 0 only if no source is related at all.
Set "found" to true if the sources answer the question, false if they don't (even if you picked a "go").

Respond with ONLY a JSON object:
{{"found": true or false, "answer": "<markdown>", "go": <number>}}"""

PAGE_SYSTEM = f"""You are Compass, an assistant in a browser side panel. The visitor is asking about the page they are looking at right now. You get that page's text, section by section, numbered.

- Do what they ask (summarize, explain, list the key points…) for THIS page only.
- For a summary: one sentence on what the page is, then 3-6 "- " bullets with the most useful points.
{RULES}

Respond with ONLY a JSON object:
{{"found": true, "answer": "<markdown>", "go": 0}}"""

SOURCE_TEMPLATE = """[{n}] PAGE: {title} ({url})
SECTION: {heading}
TEXT: {text}"""


def live_chunks(url: str, title: str, sections: list, intro: str = "", page_text: str = "") -> list:
    """The visitor's current page, shaped like site chunks. selector = the
    extension's section id ("h5"), so it can scroll straight to it.

    `intro` (meta description + text before the first heading) becomes a
    page-level chunk; `page_text` (sent for pages with almost no headings) is
    cut into windows so there is something to search."""
    base = {"url": url, "page_title": (title or "")[:200], "live": True}
    out = []
    if intro.strip():
        out.append({**base, "heading": (title or url)[:200], "level": "page", "selector": "",
                    "content": f"{title}. {intro.strip()[:1000]}"})
    for s in sections[:MAX_LIVE_SECTIONS]:
        heading = (s.get("heading") or "").strip()[:200]
        if not heading:
            continue
        text = (s.get("text") or s.get("summary") or "").strip()[:MAX_LIVE_TEXT]
        out.append({**base, "heading": heading, "level": (s.get("tag") or "h2").lower(),
                    "selector": str(s.get("id") or ""), "content": f"{heading}. {text}"})
    text = page_text.strip()[:6000]
    for i in range(0, len(text), 1200):
        out.append({**base, "heading": (title or url)[:200], "level": "p", "selector": "",
                    "content": text[i:i + 1200]})
    for i, c in enumerate(out):
        c["id"] = i
    return out


def _sources_text(chunks, chars=SOURCE_CHARS):
    return "\n\n".join(
        SOURCE_TEMPLATE.format(n=i + 1, title=c.get("page_title") or c["url"], url=c["url"],
                               heading=c["heading"], text=c["content"][:chars])
        for i, c in enumerate(chunks))


def _history_text(history: list) -> str:
    lines = []
    for h in history[-2:]:
        q = str(h.get("q", ""))[:300]
        a = str(h.get("a", ""))[:600]
        if q:
            lines.append(f"Visitor: {q}\nCompass: {a}")
    return "\n\n".join(lines)


def _source_out(c) -> dict:
    return {"url": c["url"], "title": c.get("page_title", ""), "heading": c["heading"],
            "level": c.get("level", "h2"), "id": (c["selector"] or None) if c.get("live") else None}


def _tidy(text: str) -> str:
    """Normalise citation styles some models use: 【1】, 【1†L3-L5】, [1, 2] -> [1] [2]."""
    text = re.sub(r"【\s*(\d+)[^】]*】", r"[\1]", text)
    text = re.sub(r"\[(\d+)\s*,\s*(\d+)\]", r"[\1] [\2]", text)
    return text.strip()[:MAX_ANSWER_CHARS]


def _llm(llm, system: str, user: str) -> dict:
    resp = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=2000,          # gpt-oss "thinks" first; that counts towards this too
        reasoning_effort="low",
    )
    return json.loads(resp.choices[0].message.content)


def _page_mode(question, title, here, live, history, llm) -> dict:
    """Summarize / explain the page the visitor is on."""
    chunks, total = [], 0
    for c in live:
        if total > PAGE_MODE_CHARS:
            break
        chunks.append(c)
        total += min(len(c["content"]), SOURCE_CHARS)
    user = f"THE PAGE: {title} ({here})\n\nSECTIONS:\n{_sources_text(chunks)}\n\n"
    hist = _history_text(history)
    if hist:
        user += f"RECENT CONVERSATION:\n{hist}\n\n"
    user += f"VISITOR'S REQUEST: {question}"
    try:
        parsed = _llm(llm, PAGE_SYSTEM, user)
    except Exception as e:
        print(f"[ask] LLM call failed: {type(e).__name__}: {e}")
        return {"found": False, "answer": "", "go": None, "sources": [], "mode": "page",
                "reason": "the assistant is temporarily unavailable"}
    return {"found": True, "answer": _tidy(str(parsed.get("answer", ""))), "go": None,
            "sources": [_source_out(c) for c in chunks], "mode": "page", "score": 1.0}


def ask(question: str, url: str, title: str, sections: list, history: list,
        site_chunks: list, site_vecs, llm, intro: str = "", page_text: str = "") -> dict:
    question = question.strip()[:MAX_QUESTION_CHARS]
    here = clean_url(url)
    live = live_chunks(here, title, sections, intro, page_text)

    if live and ABOUT_THIS_PAGE.search(question):
        return _page_mode(question, title, here, live, history, llm)

    pool, vec_parts = [], []
    if site_chunks:
        keep = [i for i, c in enumerate(site_chunks) if not (live and c["url"] == here)]
        pool += [site_chunks[i] for i in keep]
        vec_parts.append(site_vecs[keep])
    if live:
        pool += live
        vec_parts.append(page_vectors(live))
    if not pool:
        return {"found": False, "answer": "", "go": None, "sources": [],
                "reason": "nothing to search on this site"}
    vecs = np.vstack(vec_parts)

    results = search(question, pool, vecs, k=SOURCES_K * 4)
    if history and len(question.split()) <= 7:
        # A short follow-up ("how long does it take?") is also searched together with
        # the previous question, so "it" finds the right sections. The question's own
        # matches come first, then what the conversation adds.
        follow = f"{history[-1].get('q', '')} {question}"[:MAX_QUESTION_CHARS]
        mixed = []
        for pair in zip(results, search(follow, pool, vecs, k=SOURCES_K * 4)):
            mixed.extend(pair)
        results = mixed

    # One source per (page, heading), at most PER_PAGE per page, best first.
    seen, per_page, picked = set(), {}, []
    for c, s in results:
        key = (c["url"], c["heading"])
        if key in seen or per_page.get(c["url"], 0) >= PER_PAGE:
            continue
        seen.add(key)
        per_page[c["url"]] = per_page.get(c["url"], 0) + 1
        picked.append((c, s))
        if len(picked) == SOURCES_K:
            break

    best = max((s for _, s in picked), default=0.0)
    if best < FLOOR:
        return {"found": False, "answer": "", "go": None, "sources": [],
                "reason": "nothing on this website matches", "score": best}

    chunks = [c for c, _ in picked]
    user = (f"THE VISITOR IS ON: {title or here} ({here})\n\n"
            f"SOURCES:\n{_sources_text(chunks)}\n\n")
    hist = _history_text(history)
    if hist:
        user += f"RECENT CONVERSATION:\n{hist}\n\n"
    user += f"VISITOR'S QUESTION: {question}"

    try:
        parsed = _llm(llm, SYSTEM, user)
    except Exception as e:
        print(f"[ask] LLM call failed: {type(e).__name__}: {e}")
        # Still useful: the best match, without an explanation.
        top = chunks[0]
        return {"found": best >= 0.30, "answer": "", "go": _source_out(top) if best >= 0.30 else None,
                "sources": [_source_out(c) for c in chunks[:3]], "degraded": True, "score": best}

    try:
        go = int(parsed.get("go", 0))
    except (TypeError, ValueError):
        go = 0
    text = _tidy(str(parsed.get("answer", "")))
    if not 1 <= go <= len(chunks) and picked[0][1] >= GO_FALLBACK:
        # The model found no exact answer and named nowhere to go, but search is
        # confident about a place (e.g. the MacBook Air page for its price): offer
        # that page, from its top.
        top_url = chunks[0]["url"]
        go = next((i + 1 for i, c in enumerate(chunks) if c["url"] == top_url and c["level"] in ("page", "h1")), 1)

    return {
        "found": bool(parsed.get("found", go > 0)),
        "answer": text,
        "go": _source_out(chunks[go - 1]) if 1 <= go <= len(chunks) else None,
        "sources": [_source_out(c) for c in chunks],
        "score": best,
        "mode": "site",
    }
