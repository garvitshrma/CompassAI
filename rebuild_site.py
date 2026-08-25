"""Rebuild one site's index straight from the live web, then embed it.

Uses indexer.index_site (requests+BS4, same as /auto-register) so no
Playwright is needed. Writes sites/<site_id>/chunks.json and embeddings.npy,
exactly what load_all_sites() expects at next boot.

Usage:
    python rebuild_site.py https://openlake.in openlake.in
"""
import json
import sys

from indexer import index_site
from retrieval import SITES_DIR, embed_site

root_url, site_id = sys.argv[1], sys.argv[2]

print(f"[1/2] crawling {root_url} ...")
chunks = index_site(root_url)
if not chunks:
    sys.exit("crawl produced nothing")

d = SITES_DIR / site_id
d.mkdir(parents=True, exist_ok=True)
(d / "chunks.json").write_text(
    json.dumps(chunks, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"      {len(chunks)} chunks -> {d / 'chunks.json'}")

print(f"[2/2] embedding ...")
chunks, vecs = embed_site(site_id)
names = [c["content"].lower() for c in chunks]
for probe in ["lakshya", "garvit", "soni"]:
    n = sum(1 for t in names if probe in t)
    print(f"      contains '{probe}': {n} chunk(s)")
print("done")
