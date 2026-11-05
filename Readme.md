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
| `core/crawler.py` | Crawls a server-rendered site, saves raw HTML | `pages/<domain>/*.html` + `index.json` |
| `tools/crawler_js.py` | Same, but runs a real browser for JS-rendered sites and hover menus | `pages/<domain>/*.html` + `index.json` |
| `core/chunker.py` | Splits pages at headings, generates a CSS selector per chunk | `chunks.json` |
| `core/indexer.py` | Embeds chunks and writes a per-site index | `sites/<id>/chunks.json` + `embeddings.npy` |
| `core/retrieval.py` | Hybrid search — semantic embeddings + fuzzy lexical matching | (imported by `answer.py`) |
| `core/answer.py` | Retrieves top chunk, grounds an LLM, returns answer or refusal | grounded JSON answer |
| `navigator/views.py` | Django views behind `/query`, `/register`, `/auto-register`, `/health` | HTTP JSON API |
| `tools/register_site.py` | Crawls a site locally and uploads it to a running backend | a registered site |

---

## Run with Docker

The whole backend runs in one container. You need Docker Desktop and a Groq API key.

```bash
git clone https://github.com/garvitshrma/CompassAI.git
cd CompassAI
cp .env.example .env     # then fill in GROQ_API_KEY and DJANGO_SECRET_KEY
docker compose up
```

That's it. First build takes a few minutes (it installs dependencies and bakes
the ~90MB embedding model into the image); later starts take about 20 seconds,
spent warming the embedder and loading site indexes.

Check it is alive:

```bash
curl http://localhost:8000/health
# {"status":"ok","sites":{"openlake.in": ...}}
```

Ask it something:

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"site_id":"openlake.in","query":"what is openlake"}'
```

`Ctrl+C` stops it; `docker compose down` removes the container; `docker compose
up --build` rebuilds after a code change.

### Where things live

| What | Where | Why |
|---|---|---|
| Code | baked into the image | immutable, identical on every machine |
| Secrets | `.env` on the host, read at startup | never copied into the image |
| Site indexes | `backend/sites/`, mounted as a volume | state, must survive restarts |
| Embedding model | `/app/.model_cache` in the image | downloaded at build time, not on first request |

Nothing secret is in the image: `.env` is in `.dockerignore` and injected only
at container start. The app runs as a non-root user (`appuser`).

## Local development (without Docker)

```bash
git clone https://github.com/garvitshrma/CompassAI.git
cd CompassAI

cp .env.example .env          # fill in GROQ_API_KEY and DJANGO_SECRET_KEY

cd backend
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate   # Mac/Linux

pip install -r requirements.txt

python manage.py runserver     # http://localhost:8000
```

### Adding a site to the index

```bash
# crawl a site and upload it to a running backend
python tools/register_site.py https://example.com --api http://localhost:8000

# JS-rendered sites (React/Next/Vue) need a real browser first
playwright install chromium
python tools/crawler_js.py https://example.com/
```

Or skip all of it: point the widget at a site that is not yet registered and
`/auto-register` crawls and indexes it on demand.

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

- **Django + DRF** — HTTP API layer (`/query`, `/register`, `/auto-register`, `/health`)
- **gunicorn** — WSGI server; one worker, eight threads (site indexes, cache and locks live in one process's memory)
- **fastembed** — local ONNX embeddings (`all-MiniLM-L6-v2`, 384-dim); no torch, no GPU, no API key
- **rapidfuzz** — fuzzy lexical matching
- **numpy** — vector similarity
- **BeautifulSoup** — HTML parsing and CSS-selector generation
- **Playwright** — headless browser for JS-rendered sites (`tools/crawler_js.py`)
- **Groq** — LLM for the grounded answer layer
- **Docker + docker-compose** — one-command start, model baked in at build time
- **Vanilla JS + Shadow DOM** — embeddable widget, one script tag
- **Render + Vercel** — backend and widget hosting
- **Supabase + pgvector** — production vector store *(coming)*

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

> Prototype — updated 6 November 2026 · live on openlake.in and gdg-iitbh.vercel.app (88 chunks indexed for openlake.in)

- [x] Crawler — static sites, respects robots.txt
- [x] Crawler — JS-rendered sites and hover menus (Playwright)
- [x] Chunker — heading-scoped chunks with generated CSS selectors
- [x] Hybrid retrieval — semantic + lexical, filler-stripping, heading-level bonus
- [x] Answer layer — grounded LLM with two-stage refusal
- [x] HTTP backend — Django + DRF on gunicorn (ported from FastAPI)
- [x] Embeddable widget (Shadow DOM) — scroll-to and highlight working on live sites
- [x] Multi-site — per-site indexes, routed by hostname
- [x] Zero-config onboarding — one script tag triggers an auto-crawl and self-registration
- [x] Containerised — `docker compose up`, non-root, pinned dependencies
- [ ] Supabase pgvector integration
- [ ] First paying customer

**Known issues:** thin-content pages can over-match generic queries; individual person names on team pages aren't retrievable yet.

---

## Why this is different

Every competitor (Chatbase, SiteGPT, CustomGPT, DocsBot) answers inside a chat box. None of them move the user. Compass stores a CSS selector alongside every indexed section, which means it can scroll to and highlight the actual element. The visitor verifies with their own eyes — no need to trust a text answer.

---

*Built by Garvit Sharma — IIT Bhilai*