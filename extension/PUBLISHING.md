# Publishing Compass to the Chrome Web Store

Everything needed to go from this folder to a submitted listing.
Store images are in `store/`. The privacy policy is served by the backend at `/privacy`.

## 1. Before you submit

1. **Deploy the backend.** The extension calls `POST /site` and `POST /ask`, and links to `GET /privacy`. They live in `api.py`, `site_index.py`, `site_ask.py`, `page_query.py`, `answer.py` and `privacy.html` (and `requests` was added to `requirements.txt`). Merge them into the branch Render deploys from, then check:
   - `https://compassai-vkoe.onrender.com/health` returns `{"status":"ok",…}`
   - `https://compassai-vkoe.onrender.com/privacy` shows the policy
2. **Contact email** in `privacy.html`: done (giantgarvit@gmail.com).
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

- [ ] openlake.in: the page card says "Learning the site…", then "· N pages". **Try asking** lists other pages of the site.
- [ ] Typing a word that's only on another page (e.g. a heading from /programs) shows it under **Elsewhere on openlake.in**. Picking it opens that page and highlights the heading.
- [ ] Ask a question in your own words. The answer is a short explanation with [1] [2] citations, and **Open page and go to section** goes to the right page.
- [ ] A page that is only reachable from a dropdown menu: Compass opens the menu, points at the link, clicks it, then scrolls on the new page.
- [ ] Ask a follow-up ("and when is the deadline?"). The answer uses the earlier question as context.
- [ ] **Back to where I was** after visiting another page returns to the previous page.
- [ ] A GitHub README, an MDN page and a Wikipedia article: questions about the current page still work.
- [ ] A question that isn't on the site shows **No match** with a one-line explanation.
- [ ] `chrome://settings` shows **Can't read this page**.
- [ ] Open a site, then turn Compass off and on in `chrome://extensions`, then open the panel on that tab without refreshing. It still works.
- [ ] Turn off Wi-Fi and ask a question. You get a keyword match with the "Couldn't reach the Compass server" note.
- [ ] After 15+ minutes idle, the first question still gets answered. This is Render waking up: the panel pings `/health` when it opens.

## 2. Developer account

Go to the [Chrome Web Store Developer Dashboard](https://chrome.google.com/webstore/devconsole) and sign in with the Google account that should own Compass. Google charges a one-time registration fee and asks you to verify your contact email.

## 3. Store listing (copy and paste)

**Name:** Compass

**Summary** (max 132 characters):
> Ask any website a question. Compass explains the answer and takes you to the right page and section.

**Category:** Productivity · **Language:** English

**Description:**
> Long pages, docs and policies hide what you need. Compass sits in Chrome's side panel and finds it for you.
>
> Open Compass on any page and ask in plain words: "can I get a refund?", "where are the system requirements?", "how do I join?". Compass learns the whole website, explains the answer in a few clear sentences (with links to where each fact comes from), then opens the right page, even one hidden in a dropdown menu, scrolls there and highlights it.
>
> • Works on any website, with no setup
> • Searches every page of the site, not just the one you're on
> • Explains answers in plain language, and understands follow-up questions
> • "Back to where I was" in one click
> • Copy a link that opens the page at that section
> • Keyboard friendly: Alt+C to open, ↑ ↓ to choose, Enter to go
>
> Privacy: Compass reads only the tab you're on, and only while its panel is open. To learn a website, our server reads its public pages, never anything behind your login. Your questions are never stored. No accounts, no ads, no tracking.

**Images** (in `store/`):

| File | Store field |
|---|---|
| `icon/128.png` (already in the zip) | Store icon |
| `screenshot-1.png`, `screenshot-2.png`, `screenshot-3.png` (1280×800) | Screenshots |
| `promo-tile-440x280.png` | Small promo tile |

## 4. Privacy tab (copy and paste)

**Single purpose:**
> Compass answers the user's questions about the website they are viewing, and takes them to the page and section where the answer is.

**Permission justifications:**

| Permission | Justification |
|---|---|
| `sidePanel` | Compass's interface is shown in Chrome's side panel next to the page. |
| `storage` | Saves the user's last few questions on their device, shown as "Recent". |
| `scripting` | Adds Compass's content script to tabs that were already open before the extension was installed or enabled, so it works without reloading them. |
| Host permission `<all_urls>` and content script on all sites | The user can ask about whatever website they are on, so Compass must be able to read the page's headings and navigation, open its navigation menus to find links, follow a link when the user asks to go to another page, scroll, and draw the highlight, on any site. The content script stays idle until the side panel asks it to do something. It never types into or submits forms. |

**Remote code:** No, I am not using remote code. The server only returns JSON data. No scripts are downloaded or evaluated.

**Data usage** (tick these):

- **Website content**: yes. When the panel opens, the site's address and navigation links are sent so the server can index the site's public pages. When the user asks a question, the page's headings and the text under them, its title and address (without query string) are sent, and an AI provider (Groq) writes the answer. Content from the user's browser is not stored.
- **Web history**: yes, to be safe. The address of the page the panel is opened on is sent to the server (only while the panel is open, not in the background).
- Everything else (personal info, health, financial, authentication, personal communications, location, user activity): no.
- Tick all three certifications: no selling, no unrelated use, no creditworthiness or lending.

**Privacy policy URL:** `https://compassai-vkoe.onrender.com/privacy`

## 5. Submit

Upload the zip (Package tab), fill in the listing and privacy tabs, then **Submit for review**. Extensions that can read every site usually get a more careful review, so expect it to take longer than for simple extensions. To publish an update later, raise `version` in `package.json`, run `npm run zip` again and upload the new zip.
