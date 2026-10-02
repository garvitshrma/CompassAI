# Publishing Compass to the Chrome Web Store

Everything needed to go from this folder to a submitted listing.
Store images are in `store/`. The privacy policy is served by the backend at `/privacy`.

## 1. Before you submit

1. **Deploy the backend.** The extension calls `POST /page-query` and links to `GET /privacy`. Both are new in `api.py`, `page_query.py`, `answer.py` and `privacy.html`. Merge them into the branch Render deploys from, then check:
   - `https://compassai-vkoe.onrender.com/health` returns `{"status":"ok",…}`
   - `https://compassai-vkoe.onrender.com/privacy` shows the policy
2. **Put your contact email** in `privacy.html` (search for `[YOUR CONTACT EMAIL]`), then redeploy.
3. **Delete `public/wxt.svg`.** It's a leftover from the WXT template and would otherwise be shipped.
4. **Test the production build**, which behaves like the store version:
   ```bash
   npm run build
   ```
   In `chrome://extensions`, turn off the dev copy, click **Load unpacked**, and choose `.output/chrome-mv3`. Then go through the checklist below.
5. **Make the zip:**
   ```bash
   npm run zip
   ```
   The file to upload is `.output/compass-extension-0.1.0-chrome.zip`.

### Test checklist (production build)

- [ ] openlake.in, a GitHub README, an MDN page, a Wikipedia article and one docs site: headings appear, a question finds a sensible section, **Go to section** scrolls and highlights, **Back** returns.
- [ ] A question that isn't on the page shows **No match**.
- [ ] `chrome://settings` shows **Can't read this page**.
- [ ] Open a site, then turn Compass off and on in `chrome://extensions`, then open the panel on that tab without refreshing. It still works.
- [ ] Turn off Wi-Fi and ask a question. You get a keyword match with the "Couldn't reach the Compass server" note.
- [ ] After 15+ minutes idle, the first question still gets answered. This is Render waking up: the panel pings `/health` when it opens.

## 2. Developer account

Go to the [Chrome Web Store Developer Dashboard](https://chrome.google.com/webstore/devconsole) and sign in with the Google account that should own Compass. Google charges a one-time registration fee and asks you to verify your contact email.

## 3. Store listing (copy and paste)

**Name:** Compass

**Summary** (max 132 characters):
> Ask any website where something is. Compass scrolls you to the right section and highlights it.

**Category:** Productivity · **Language:** English

**Description:**
> Long pages, docs and policies hide what you need. Compass sits in Chrome's side panel and finds it for you.
>
> Open Compass on any page and ask in plain words: "can I get a refund?", "where are the system requirements?", "how do I join?". Compass reads the page's sections, picks the one that answers you, scrolls the page there and highlights it.
>
> • Works on any website, with no setup
> • Understands meaning, not just exact words
> • "Back to where I was" in one click
> • Copy a link that opens the page at that section
> • Keyboard friendly: Alt+C to open, ↑ ↓ to choose, Enter to go
>
> Privacy: Compass reads only the tab you're on, and only while its panel is open. Nothing leaves your browser until you ask a question. Then only your question and the page's headings are sent, and they're never stored. No accounts, no ads, no tracking.

**Images** (in `store/`):

| File | Store field |
|---|---|
| `icon/128.png` (already in the zip) | Store icon |
| `screenshot-1.png`, `screenshot-2.png`, `screenshot-3.png` (1280×800) | Screenshots |
| `promo-tile-440x280.png` | Small promo tile |

## 4. Privacy tab (copy and paste)

**Single purpose:**
> Compass helps the user find and jump to the section of the current web page that answers their question.

**Permission justifications:**

| Permission | Justification |
|---|---|
| `sidePanel` | Compass's interface is shown in Chrome's side panel next to the page. |
| `storage` | Saves the user's last few questions on their device, shown as "Recent". |
| `scripting` | Adds Compass's content script to tabs that were already open before the extension was installed or enabled, so it works without reloading them. |
| Host permission `<all_urls>` and content script on all sites | The user can ask about whatever page they are on, so Compass must be able to read that page's headings, scroll it and draw the highlight on any site. The content script stays idle until the user asks from the side panel. It never submits forms or clicks anything. |

**Remote code:** No, I am not using remote code. The server only returns JSON data. No scripts are downloaded or evaluated.

**Data usage** (tick these):

- **Website content**: yes. When the user asks a question, the page's headings, the short text under them, its title and its address (without query string) are sent to the Compass server, which uses an AI provider (Groq) to choose the best section. None of it is stored.
- Everything else (personal info, health, financial, authentication, personal communications, location, web history, user activity): no.
- Tick all three certifications: no selling, no unrelated use, no creditworthiness or lending.

**Privacy policy URL:** `https://compassai-vkoe.onrender.com/privacy`

## 5. Submit

Upload the zip (Package tab), fill in the listing and privacy tabs, then **Submit for review**. Extensions that can read every site usually get a more careful review, so expect it to take longer than for simple extensions. To publish an update later, raise `version` in `package.json`, run `npm run zip` again and upload the new zip.
