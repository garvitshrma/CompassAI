"""Purge non-production (localhost) chunks from every site index, renumber,
clean meta.json hashes, and re-embed affected sites."""
import json
from pathlib import Path

from retrieval import SITES_DIR, embed_site

BAD_PREFIXES = ("http://localhost", "https://localhost", "http://127.0.0.1")

for d in sorted(SITES_DIR.iterdir()):
    cj = d / "chunks.json"
    if not cj.exists():
        continue
    chunks = json.loads(cj.read_text(encoding="utf-8"))
    clean = [c for c in chunks
             if not str(c.get("url", "")).lower().startswith(BAD_PREFIXES)]
    dropped = len(chunks) - len(clean)
    if dropped:
        for i, c in enumerate(clean):
            c["id"] = i
        cj.write_text(json.dumps(clean, indent=2, ensure_ascii=False),
                      encoding="utf-8")
        meta_p = d / "meta.json"
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
                for u in [k for k in meta.get("hashes", {}) if k.startswith("http://localhost")]:
                    del meta["hashes"][u]
                meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print(f"  meta.json cleanup skipped ({e})")
        embed_site(d.name)
        print(f"{d.name}: dropped {dropped}, {len(clean)} remain, re-embedded")
    else:
        print(f"{d.name}: clean ({len(chunks)} chunks)")
