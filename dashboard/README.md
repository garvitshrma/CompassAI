# dashboard

The site-owner dashboard: React + Vite + Tailwind. **Not built yet** — this
folder is a placeholder so the layout is visible in git (git tracks files, not
directories, so an empty folder would simply not exist in a clone).

## What goes here

A standalone Vite app that talks to the Django backend over HTTP. It is where a
site owner will:

- sign up and register a site
- see what Compass has indexed (pages, sections, last crawl)
- add facts the crawler cannot find ("fees are X", "contact Y for admissions")
- get the script tag to paste into their site

## What does NOT go here

**The widget.** `widget/compass-widget.js` is deliberately dependency-free
vanilla JS, because it gets injected into other people's pages — it cannot
carry React, and it has to survive inside a Shadow DOM on a site whose own
React version you do not control. The dashboard and the widget are two separate
frontends that happen to talk to the same API.

**Django templates.** Django does not serve this app. It builds to static files
and deploys separately (Vercel), exactly like the existing frontend work.

## When it gets built

Step 4 of the current plan, after the move to Postgres + pgvector. It needs
real tables to read and write, and `django.contrib.auth` for accounts — neither
of which exists yet.

## Setup, when the time comes

```
npm create vite@latest . -- --template react
npm install
npm run dev
```
