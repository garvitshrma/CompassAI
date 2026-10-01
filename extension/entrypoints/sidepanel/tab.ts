// Reads the page in the active tab by asking its content script.

export interface Heading {
  tag: string; // "H1" | "H2" | "H3"
  text: string;
  position: number; // 0 = top of the page, 1 = bottom
  summary: string;
  anchor: string; // "#refunds" if the heading has an id, else ""
}

export interface PageRead {
  url: string;
  title: string;
  screens: number;
  viewTop: number;
  headings: Heading[];
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
