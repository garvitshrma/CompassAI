// Reads the page in the active tab by asking its content script.

export interface Heading {
  tag: string; // "H1" | "H2" | "H3"
  text: string;
  position: number; // 0 = top of the page, 1 = bottom
  summary: string; // first ~200 characters under the heading
  body: string; // up to ~1200 characters under the heading
  anchor: string; // "#refunds" if the heading has an id, else ""
}

export interface PageRead {
  url: string;
  title: string;
  screens: number;
  viewTop: number;
  headings: Heading[];
  intro: string; // meta description + text before the first heading
  text: string; // the page's text, only when it has fewer than 3 headings
}

export type TabRead =
  | ({ ok: true; tabId: number } & PageRead)
  | { ok: false; url: string; reason: 'restricted' | 'failed' };

// Pages Chrome never lets extensions read.
function isRestricted(url: string) {
  return (
    url === '' ||
    /^(chrome|chrome-extension|edge|about|devtools|view-source):/.test(url) ||
    url.startsWith('https://chromewebstore.google.com')
  );
}

function ask(tabId: number): Promise<PageRead> {
  return browser.tabs.sendMessage(tabId, { type: 'GET_PAGE' });
}

/** The URL of the active tab, without reading the page. */
export async function activeTabUrl(): Promise<string> {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  return tab?.url ?? '';
}

/** A link from the site's navigation (see GET_LINKS in content.ts). */
export interface NavLink {
  url: string;
  text: string;
  menu: string; // the dropdown it sits in ("" = none)
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Sends a message to the content script in a tab, waiting for it to be ready.
 * Right after a page loads the content script may not be there yet, so we retry,
 * and if it never appears (a tab opened before Compass was installed) we inject it.
 */
export async function messageTab<T>(tabId: number, message: object, ms = 6000): Promise<T | null> {
  const until = Date.now() + ms;
  let injected = false;
  for (let attempt = 0; Date.now() < until; attempt++) {
    try {
      return (await browser.tabs.sendMessage(tabId, message)) as T;
    } catch {
      if (attempt >= 3 && !injected) {
        injected = true;
        await browser.scripting.executeScript({ target: { tabId }, files: ['/content-scripts/content.js'] }).catch(() => {});
      }
      await wait(300);
    }
  }
  return null;
}

/** The navigation links of the page in a tab, including the ones in dropdown menus. */
export async function readLinks(tabId: number): Promise<NavLink[]> {
  return (await messageTab<NavLink[]>(tabId, { type: 'GET_LINKS' }, 8000)) ?? [];
}

/**
 * Waits until the tab has loaded a different page. Resolves true once it's loaded
 * (or false after `ms`, or when `signal` aborts).
 */
export async function waitForNewPage(tabId: number, fromUrl: string, toUrl: string, signal: AbortSignal, ms = 20000): Promise<boolean> {
  const start = Date.now();
  const bare = (u: string) => (u.split(/[#?]/)[0] ?? '').replace(/\/$/, '');
  while (Date.now() - start < ms && !signal.aborted) {
    await wait(250);
    const tab = await browser.tabs.get(tabId).catch(() => null);
    if (!tab?.url) continue;
    const arrived = bare(tab.url) === bare(toUrl);
    const movedSomewhere = bare(tab.url) !== bare(fromUrl) && Date.now() - start > 1500; // e.g. a redirect
    if (tab.status === 'complete' && (arrived || movedSomewhere)) return true;
  }
  return false;
}

export async function readActiveTab(): Promise<TabRead> {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url ?? '';
  if (!tab?.id || isRestricted(url)) return { ok: false, url, reason: 'restricted' };

  try {
    return { ok: true, tabId: tab.id, ...(await ask(tab.id)) };
  } catch {
    // Nobody answered: this tab was open before Compass was installed or enabled.
    // Put the content script in now, then ask again.
    console.info('[Compass] injecting content script into tab', tab.id);
    try {
      await browser.scripting.executeScript({
        target: { tabId: tab.id },
        files: ['/content-scripts/content.js'],
      });
      return { ok: true, tabId: tab.id, ...(await ask(tab.id)) };
    } catch (err) {
      console.warn('[Compass] could not read tab', err);
      return { ok: false, url, reason: 'failed' };
    }
  }
}
