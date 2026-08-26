# Compass

> An AI assistant that takes website visitors to the exact place they asked for.

Most AI site chatbots answer questions inside a chat box. Compass is different — it physically navigates the visitor to the right section of the page, scrolls to it, highlights it, and explains it. One script tag for the site owner. Zero friction for the visitor.

---

## The problem

People land on websites and can't find what they're looking for. They leave. The website loses a lead.

A gym visitor can't find the coach list. A student can't find the admission fee. A patient can't find the doctor's schedule. They don't need a chatbot — they need someone to take them there.

---

## What Compass does

A visitor types: *"where are the fees"* or *"show me the coaches"* or *"how do I apply"*

Compass finds the right section on the site, scrolls to it, highlights it, and explains what's there. Not an answer in a box — actual navigation.

---

## How it works

```
Site owner adds one script tag
        ↓
Compass crawls and indexes the site (headings, sections, CSS selectors)
        ↓
Visitor asks a question in plain language
        ↓
Hybrid search (semantic + lexical) finds the best matching section
        ↓
LLM generates a grounded explanation — or refuses if nothing fits
        ↓
Widget scrolls and highlights the exact element on the page
```

The CSS selector stored alongside each chunk is what makes navigation possible. Every other product in this space returns text. Compass returns a location.

---

## Pipeline

| Script | What it does | Output |
|--------|-------------|--------|
| `crawler.py` | Crawls a server-rendered site, saves raw HTML | `pages/<domain>/*.html` + `index.json` |
| `crawler_js.py` | Same, but runs a real browser for JS-rendered sites and hover menus | `pages/<domain>/*.html` + `index.json` |
| `chunker.py` | Splits pages at headings, generates a CSS selector per chunk | `chunks.json` |
| `retrieval.py` | Hybrid search — semantic embeddings + fuzzy lexical matching | (imported by `answer.py`) |
| `answer.py` | Retrieves top chunk, grounds an LLM, returns answer or refusal | interactive query loop |
| `search.py` | Standalone semantic-only search (superseded by `retrieval.py`) | `embeddings.npy` + query loop |
| `rebuild_site.py` | Re-crawl one site from the live web and rebuild its `sites/<id>/` index | `chunks.json` + `embeddings.npy` |

---

## Quick start

```bash
git clone https://github.com/garvitshrma/compass
cd compass

python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Mac/Linux

pip install -r requirements.txt

# For JS-rendered sites, install the browser once:
playwright install chromium

# 1. Crawl a site  (use crawler_js.py for React/Next/Vue sites)
python crawler_js.py https://openlake.in/
#    then copy the per-domain folder up so the chunker finds it:
#    (Windows)  Copy-Item pages\openlake.in\* pages\ -Force

# 2. Chunk it
python chunker.py

# 3. Answer questions  (needs a free OpenRouter API key)
# Get one at https://openrouter.ai/keys
setx OPENROUTER_API_KEY "sk-or-v1-..."   # then reopen the terminal
python answer.py
```

---

## How retrieval works

Compass combines two signals, because neither alone is enough:

- **Semantic** — sentence-transformer embeddings, cosine similarity. Catches meaning when the words don't match (query "codeforces" finds the project "canonforces").
- **Lexical** — fuzzy string matching over the heading, page title, and URL slug. Catches typos ("phylosophy") and exact page names.

On top of that:
- **Filler-word stripping** — navigation queries like "take me to the projects page" get reduced to "projects" before lexical matching.
- **Heading-level bonus** — an `h1` (a page's identity) is nudged above buried section headings, so page-level queries land at the top of the right page.
- **Two-stage refusal** — if the top score is below a floor, Compass refuses without even calling the LLM; if the LLM judges the chunk irrelevant, it refuses too. This is the anti-hallucination guarantee: no confident wrong answers.

---

## Stack

- **Python** — crawler, chunker, retrieval, answer pipeline
- **BeautifulSoup** — HTML parsing and CSS-selector generation
- **Playwright** — headless browser for JS-rendered sites
- **sentence-transformers** — local embeddings (`all-MiniLM-L6-v2`)
- **rapidfuzz** — fuzzy lexical matching
- **numpy** — vector similarity
- **Switchable LLM provider** — Gemini (primary) with automatic failover, via `.env` (`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`); also runs on Groq, OpenRouter, or local Ollama
- **FastAPI** — API layer *(coming)*
- **Supabase + pgvector** — production vector store *(coming)*
- **Vanilla JS + Shadow DOM** — embeddable widget *(coming)*

---

## Roadmap

**Phase 1 — Web widget** *(in progress)*
Embeddable widget, one script tag, works on any site. Target: coaching institutes, clinics, college sites.

**Phase 2 — Browser extension**
Works on any site without the owner installing anything.

**Phase 3 — Universal OS agent**
Screen awareness, works in any app on any computer. Cross-platform from day one.

---

## Status

> Prototype — updated 7 August 2026 · validated end-to-end on openlake.in (136 chunks across 10 pages)

- [x] Crawler — static sites, respects robots.txt
- [x] Crawler — JS-rendered sites and hover menus (Playwright)
- [x] Chunker — heading-scoped chunks with generated CSS selectors
- [x] Hybrid retrieval — semantic + lexical, filler-stripping, heading-level bonus
- [x] Answer layer — grounded LLM with two-stage refusal
- [ ] FastAPI backend
- [ ] Supabase pgvector integration
- [ ] Embeddable widget (Shadow DOM)
- [ ] First paying customer

**Known issues:** thin-content pages can over-match generic queries. Person names ARE retrievable when they exist on the site (rarity-weighted content matching); people who aren't on the site correctly refuse rather than guess.

---

## Why this is different

Every competitor (Chatbase, SiteGPT, CustomGPT, DocsBot) answers inside a chat box. None of them move the user. Compass stores a CSS selector alongside every indexed section, which means it can scroll to and highlight the actual element. The visitor verifies with their own eyes — no need to trust a text answer.

---

*Built by Garvit Sharma — IIT Bhilai*