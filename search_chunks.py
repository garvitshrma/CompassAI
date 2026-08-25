import json
with open('sites/openlake.in/chunks.json', encoding='utf-8') as f:
    chunks = json.load(f)
for i, c in enumerate(chunks):
    txt = (c.get('heading') or '') + ' ' + (c.get('url') or '') + ' ' + (c.get('text') or '')
    lower = txt.lower()
    if 'community' in lower or 'tulsyan' in lower or 'slok' in lower or 'past' in lower:
        print(f'Line {i}: heading={c.get("heading")}, url={c.get("url")}')
print('Search done')