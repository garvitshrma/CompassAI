// Everything the side panel knows about "the page", "the website" and "the answer" comes through this file.
//
//   - analyzePage  reads the active tab's headings (via the content script, see tab.ts)
//   - loadSite     reads the site's navigation (dropdowns included) and asks the backend's /site
//                  for a map of the whole website, used for suggestions across all its pages
//   - askCompass   sends the question to the backend's /ask, which explains the answer from the
//                  whole website and picks the best place to go. Falls back to keyword
//                  matching on this page if the server can't be reached.
//   - navigateTo   scrolls to a section on this page, or opens another page (through its
//                  dropdown menu if it has one) and then scrolls to the section there
//
// A Section can be on the current page or on another page of the site: `url` says which page.

import type { SuggestionIcon } from './icons';
import { activeTabUrl, messageTab, readActiveTab, readLinks, waitForNewPage, type NavLink, type TabRead } from './tab';
import { ANSWER_TIMEOUT_MS, API_URL } from './config';

export type Confidence = 'high' | 'medium' | 'low';

export interface Section {
  id: string;
  level: string;         // "H1" | "H2" | "H3", or "page" = the page itself (no heading to scroll to)
  number: string;        // "§7" (real pages have none)
  group: string;         // the heading above it, or the page's name for other pages
  title: string;         // "Refunds and Cancellations"
  noun: string;          // "“Refunds and Cancellations”" -> "I found “Refunds…”."
  summary: string;
  text: string;          // the text under the heading (only for the current page)
  points: string[];      // shown under "In this section"
  anchor: string;        // "#refunds" if the heading has an id
  position: number;      // 0 = top of the page, 1 = bottom (current page only)
  url: string;           // the page it's on: origin + path
  pageTitle: string;
  menu: string;          // the dropdown menu that links to its page, e.g. "Programs"
}

export interface PageLink {
  label: string;
  url: string;
  keywords: string[];
  why: string;
  alternatives: string[];
}

export interface Suggestion {
  text: string;
  icon: SuggestionIcon;
}

export interface PageInfo {
  tabId: number | null;  // the browser tab this page is in (null for restricted pages)
  url: string;
  title: string;         // the page's <title>
  domain: string;
  path: string;
  badge: string;         // two letters shown in the favicon box
  restricted: boolean;   // chrome:// pages etc. — extensions can't read them
  screens: number;       // page height in screenfuls, for "about 3 screens down"
  viewTop: number;       // where the person is scrolled to right now (0–1)
  sections: Section[];
  intro: string;         // meta description + text before the first heading
  text: string;          // the whole page's text, only for pages with almost no headings
  links: PageLink[];
  suggestions: Suggestion[];
}

/** What Compass knows about the rest of the website. */
export interface SiteInfo {
  domain: string;
  state: 'loading' | 'indexing' | 'ready' | 'failed' | 'offline';
  pages: number;
  sections: Section[];   // headings across all pages (from the server's site map)
  links: NavLink[];      // navigation links read from the live page, with their dropdown menus
}

export interface Match {
  section: Section;
  confidence: Confidence;
}

export type QueryResult =
  | {
      kind: 'found';
      headline: string;
      best?: Match;         // where to go; none for answers about this page ("summarize this page")
      others: Match[];
      answer?: string;      // the explanation, in simple markdown, with [n] citations
      sources?: Section[];  // what [1], [2]… refer to
      explanation?: string;
      offline?: boolean;    // true = the server couldn't be reached; this is keyword matching
    }
  | {
      kind: 'none';
      topic: string;
      detail: string;
      answer?: string;
      link?: PageLink;
      closest?: Match;
      alternatives: string[];
      offline?: boolean;
    };

/** One earlier question and answer, so follow-ups like "how long does it take?" make sense. */
export interface Turn {
  q: string;
  a: string;
}

/* ------------------------------------------------------------------ */
/* URLs                                                                */
/* ------------------------------------------------------------------ */

/** origin + path, without query, #fragment or trailing slash. Query strings can hold
 *  session ids or personal data, so this is also the only form that leaves the browser. */
export function bareUrl(url: string) {
  try {
    const u = new URL(url);
    return (u.origin + u.pathname).replace(/\/$/, '');
  } catch {
    return '';
  }
}

export const onThisPage = (page: PageInfo, section: Section) => bareUrl(section.url) === bareUrl(page.url);

/** "https://lumen.dev/programs/summer-of-code" -> "/programs/summer-of-code" (or "Home") */
export function pathOf(url: string) {
  try {
    const path = new URL(url).pathname.replace(/\/$/, '');
    return path || 'Home';
  } catch {
    return url;
  }
}

/** "Summer of Code" + "/programs/summer-of-code", without saying the same thing twice. */
export function pageLabel(section: Pick<Section, 'pageTitle' | 'url'>) {
  const name = pageName(section);
  const path = pathOf(section.url);
  return name === path ? name : `${name} · ${path}`;
}

/** "Summer of Code · Lumen Club" -> "Summer of Code" */
export function pageName(section: Pick<Section, 'pageTitle' | 'url'>) {
  const t = (section.pageTitle.split(/\s+[·|–—-]\s+/)[0] ?? '').trim();
  return t || pathOf(section.url);
}

function splitUrl(url: string) {
  try {
    const u = new URL(url);
    if (!u.protocol.startsWith('http')) return { domain: url, path: '' };
    return { domain: u.hostname.replace(/^www\./, ''), path: u.pathname === '/' ? '' : u.pathname };
  } catch {
    return { domain: url || 'This page', path: '' };
  }
}

/* ------------------------------------------------------------------ */
/* Turning what the content script read into a PageInfo               */
/* ------------------------------------------------------------------ */

/** A page with nothing read yet: just enough for the page card while analyzing. */
function shell(url: string, restricted = false): PageInfo {
  const { domain, path } = splitUrl(url);
  return {
    tabId: null,
    url,
    title: '',
    domain,
    path,
    badge: restricted ? '' : domain.slice(0, 2).toLowerCase(),
    restricted,
    screens: 1,
    viewTop: 0,
    sections: [],
    intro: '',
    text: '',
    links: [],
    suggestions: [],
  };
}

function toPageInfo(read: Extract<TabRead, { ok: true }>): PageInfo {
  const page = shell(read.url);
  page.tabId = read.tabId;
  page.title = read.title;
  const rank = (tag: string) => Number(tag[1]); // "H2" -> 2

  page.screens = Math.max(read.screens, 1);
  page.viewTop = read.viewTop;
  page.intro = read.intro ?? '';
  page.text = read.text ?? '';
  page.sections = read.headings.map((h, i) => {
    // The group is the nearest heading above this one with a higher rank, e.g. the H2 above an H3.
    const parent = read.headings.slice(0, i).reverse().find((p) => rank(p.tag) < rank(h.tag));
    return {
      id: `h${i}`,
      level: h.tag,
      number: '',
      group: parent?.text ?? read.title,
      title: h.text,
      noun: `“${h.text}”`,
      summary: h.summary,
      text: h.body ?? h.summary,
      points: [],
      anchor: h.anchor,
      position: Math.min(Math.max(h.position, 0), 1),
      url: bareUrl(read.url),
      pageTitle: read.title,
      menu: '',
    };
  });
  return page;
}

/* ------------------------------------------------------------------ */
/* Text helpers                                                        */
/* ------------------------------------------------------------------ */

const STOP = new Set(
  'a an and are can do does find for from get go how i in is it me my of on or page please show take that the there this to what where which who why with you your about any tell explain'.split(' '),
);

/** "Where can I find the refund policy?" -> ["refund", "policy"] */
export function keywords(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((w) => w.length >= 3 && !STOP.has(w))
    .map((w) => (w.length > 4 && w.endsWith('s') ? w.slice(0, -1) : w));
}

const has = (text: string, word: string) => text.toLowerCase().includes(word);

function scoreSection(section: Section, words: string[]): number {
  let score = 0;
  for (const w of words) {
    if (has(section.title, w)) score += 3;
    if (has(section.summary, w) || section.points.some((p) => has(p, w))) score += 1;
  }
  return score;
}

const toConfidence = (score: number): Confidence => (score >= 4 ? 'high' : score >= 2 ? 'medium' : 'low');

/**
 * Instant suggestions while the person types: matching headings on this page and on
 * the rest of the website. Runs on this computer, no server needed.
 */
export function matchHeadings(page: PageInfo, text: string, site?: SiteInfo | null): { word: string; here: Section[]; elsewhere: Section[] } {
  const words = keywords(text);
  if (!words.length) return { word: '', here: [], elsewhere: [] };
  const longest = [...words].sort((a, b) => b.length - a.length)[0] ?? '';

  const score = (s: Section) => {
    let n = 0;
    for (const w of words) {
      if (has(s.title, w)) n += 3;
      if (has(s.pageTitle, w) || has(pathOf(s.url), w) || has(s.menu, w)) n += 1;
    }
    return n;
  };
  const rank = (list: Section[]) =>
    list
      .map((s) => ({ s, n: score(s) }))
      .filter((r) => r.n >= 3 || (r.n >= 1 && r.s.level === 'page'))
      .sort((a, b) => b.n - a.n)
      .map((r) => r.s);

  const here = rank(page.sections).slice(0, 4);
  const seen = new Set<string>();
  const elsewhere = rank((site?.sections ?? []).filter((s) => !onThisPage(page, s)))
    .filter((s) => {
      const key = s.url + '|' + s.title;
      return !seen.has(key) && seen.add(key);
    })
    .slice(0, 5);
  return { word: longest, here, elsewhere };
}

/** "Try asking" on the Home screen: this page, plus the main pages of the site. */
export function suggestionsFor(page: PageInfo, site: SiteInfo | null): Suggestion[] {
  const out: Suggestion[] = [];
  if (page.sections.length >= 2) out.push({ text: 'Summarize this page', icon: 'checklist' });

  // One suggestion per other page, preferring pages that are in the site's menus.
  const pages = new Map<string, Section>();
  for (const s of site?.sections ?? []) {
    if (onThisPage(page, s) || pages.has(s.url)) continue;
    pages.set(s.url, s);
  }
  const ranked = [...pages.values()].sort((a, b) => Number(!!b.menu) - Number(!!a.menu));
  for (const s of ranked.slice(0, 3)) out.push({ text: `Take me to ${pageName(s)}`, icon: 'hash' });

  // Not enough of the site known yet: fall back to this page's main headings.
  if (out.length < 3) {
    const main = page.sections.filter((s) => s.level === 'H2');
    for (const s of (main.length ? main : page.sections).slice(0, 4 - out.length)) out.push({ text: `Take me to ${s.title}`, icon: 'hash' });
  }
  return out.slice(0, 4);
}

/** How far a section is from a point on the page, in words: "About 3 screens down". */
export function distance(page: PageInfo, from: number, to: number): string {
  const screens = Math.round((to - from) * page.screens);
  if (screens === 0) return 'On this screen';
  const n = Math.abs(screens);
  return `${n} screen${n === 1 ? '' : 's'} ${screens > 0 ? 'down' : 'up'}`;
}

/** Where a section is, from the reader's point of view: "2 screens down" or "On Summer of Code". */
export function whereIs(page: PageInfo, section: Section, from = page.viewTop): string {
  if (onThisPage(page, section)) return distance(page, from, section.position);
  return section.menu ? `${section.menu} › ${pageName(section)}` : `On ${pageName(section)}`;
}

/** Joins the non-empty parts: label('§7', 'Billing') -> "§7 · Billing", label('', 'Billing') -> "Billing". */
export const label = (...parts: string[]) => parts.filter(Boolean).join(' · ');

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ */
/* Reading the page and the site                                       */
/* ------------------------------------------------------------------ */

/**
 * Reads the active tab. `onStep` reports progress for the Loading screen:
 * 0 = started (with a shell page that only has the URL), 3 = done.
 */
export async function analyzePage(onStep: (step: number, shell: PageInfo) => void): Promise<PageInfo> {
  const url = await activeTabUrl();
  const loading = shell(url);
  onStep(0, loading);

  const read = await readActiveTab();
  if (!read.ok) return shell(read.url, true);

  onStep(3, loading);
  return toPageInfo(read);
}

/** A row of the server's site map, turned into a Section on another page. */
function siteSection(row: { url: string; title: string; heading: string; level: string }, menus: Map<string, string>, i: number): Section {
  const page = row.level === 'page' || row.level === 'p';
  return {
    id: `s${i}`,
    level: page ? 'page' : row.level.toUpperCase(),
    number: '',
    group: '',
    title: page ? pageName({ pageTitle: row.title, url: row.url }) || row.heading : row.heading,
    noun: `“${row.heading}”`,
    summary: '',
    text: '',
    points: [],
    anchor: '',
    position: 0,
    url: bareUrl(row.url),
    pageTitle: row.title,
    menu: menus.get(bareUrl(row.url)) ?? '',
  };
}

interface SiteReply {
  state: 'indexing' | 'ready' | 'failed';
  pages: number;
  map: { url: string; title: string; heading: string; level: string }[];
}

async function postSite(url: string, links: NavLink[]): Promise<SiteReply> {
  const res = await fetch(`${API_URL}/site`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: bareUrl(url), links }),
  });
  if (!res.ok) throw new Error(`server answered ${res.status}`);
  return res.json();
}

// The panel stays open while the person browses, so remember each site for a while.
const siteCache = new Map<string, { site: SiteInfo; at: number }>();

/**
 * Learns the website the page belongs to:
 *  1. reads its navigation in the live tab, opening dropdown menus to find hidden links
 *  2. sends those links to the server, which reads the site's public pages (first time only)
 *  3. while the server is still reading, asks again every few seconds
 * Calls `onUpdate` every time it knows more. Returns a function that stops it.
 */
export function loadSite(page: PageInfo, onUpdate: (site: SiteInfo) => void): () => void {
  let stopped = false;
  const cached = siteCache.get(page.domain);
  if (cached && cached.site.state === 'ready' && Date.now() - cached.at < 10 * 60_000) {
    onUpdate(cached.site);
    return () => {};
  }

  (async () => {
    let site: SiteInfo = { domain: page.domain, state: 'loading', pages: 0, sections: [], links: [] };
    onUpdate(site);
    if (page.tabId === null) return;

    const links = await readLinks(page.tabId);
    const menus = new Map(links.filter((l) => l.menu).map((l) => [bareUrl(l.url), l.menu]));
    // Pages from the menus are useful even before the server answers.
    const fromLinks = links.map((l, i) =>
      siteSection({ url: l.url, title: l.text, heading: l.text, level: 'page' }, menus, 1000 + i),
    );
    site = { ...site, links, sections: fromLinks };
    if (!stopped) onUpdate(site);

    for (let poll = 0; poll < 45 && !stopped; poll++) {
      try {
        const reply = await postSite(page.url, poll === 0 ? links : []);
        const sections = reply.map.map((row, i) => siteSection(row, menus, i));
        // Menu pages the server doesn't have (yet) stay in the list.
        const known = new Set(sections.map((s) => s.url));
        site = {
          ...site,
          state: reply.state,
          pages: reply.pages,
          sections: [...sections, ...fromLinks.filter((s) => !known.has(s.url))],
        };
      } catch (err) {
        console.warn('[Compass] /site failed:', err);
        site = { ...site, state: 'offline' };
      }
      if (stopped) return;
      onUpdate(site);
      if (site.state !== 'indexing') break;
      await wait(4000);
    }
    if (site.state === 'ready') siteCache.set(page.domain, { site, at: Date.now() });
  })();

  return () => {
    stopped = true;
  };
}

/* ------------------------------------------------------------------ */
/* Answering                                                           */
/* ------------------------------------------------------------------ */

/** Matches the question against heading words, on this computer. Used when the server can't be reached. */
function askLocally(page: PageInfo, question: string): QueryResult {
  const words = keywords(question);
  const ranked = page.sections
    .map((section) => ({ section, score: scoreSection(section, words) }))
    .filter((r) => r.score > 0)
    .sort((a, b) => b.score - a.score);

  const [best, ...rest] = ranked;
  if (best && best.score >= 2) {
    return {
      kind: 'found',
      headline: `I found ${best.section.noun}.`,
      best: { section: best.section, confidence: toConfidence(best.score) },
      others: rest.filter((r) => r.score >= 2).slice(0, 3).map((r) => ({ section: r.section, confidence: toConfidence(r.score) })),
    };
  }

  const topic = words.join(' ') || question.trim();
  return {
    kind: 'none',
    topic,
    detail: `Compass checked all ${page.sections.length} sections.`,
    closest: best ? { section: best.section, confidence: 'low' } : undefined,
    alternatives: [],
  };
}

/** What the backend's /ask sends back (see site_ask.py). */
interface Source {
  url: string;
  title: string;
  heading: string;
  level: string;
  id: string | null; // our section id ("h4") when it's from the live page
}
interface AskReply {
  found: boolean;
  reason?: string;
  mode?: 'page' | 'site';
  answer: string;
  go: Source | null;
  sources: Source[];
  score?: number;
  degraded?: boolean;
}

const squash = (t: string) => t.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

/** Finds the Section a source refers to: one on this page if possible, else one on another page. */
function toSection(page: PageInfo, site: SiteInfo | null, src: Source, i: number): Section {
  if (src.id) {
    const live = page.sections.find((s) => s.id === src.id);
    if (live) return live;
  }
  if (bareUrl(src.url) === bareUrl(page.url)) {
    const live = page.sections.find((s) => squash(s.title) === squash(src.heading));
    if (live) return live;
  }
  const menus = new Map((site?.links ?? []).filter((l) => l.menu).map((l) => [bareUrl(l.url), l.menu]));
  return siteSection(src, menus, 2000 + i);
}

async function askServer(page: PageInfo, question: string, history: Turn[]): Promise<AskReply> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ANSWER_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_URL}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: ctrl.signal,
      body: JSON.stringify({
        question,
        url: bareUrl(page.url),
        title: page.title,
        // This page's headings and the text under them (read in your browser, so it
        // includes content that JavaScript drew). Never form fields or the full HTML.
        sections: page.sections.map((s) => ({ id: s.id, tag: s.level, heading: s.title, text: s.text || s.summary })),
        intro: page.intro,
        page_text: page.text,
        history: history.slice(-2).map((t) => ({ q: t.q, a: t.a.slice(0, 600) })),
      }),
    });
    if (!res.ok) throw new Error(`server answered ${res.status}`);
    const data = (await res.json()) as AskReply;
    if (data.reason === 'the assistant is temporarily unavailable') throw new Error(data.reason);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

const scoreToConfidence = (score = 0): Confidence => (score >= 0.5 ? 'high' : score >= 0.3 ? 'medium' : 'low');

function fromServer(page: PageInfo, site: SiteInfo | null, question: string, data: AskReply): QueryResult {
  const sources = data.sources.map((src, i) => toSection(page, site, src, i));
  const goIndex = data.go ? data.sources.findIndex((s) => s.url === data.go!.url && s.heading === data.go!.heading) : -1;
  const best = goIndex >= 0 ? sources[goIndex] : data.go ? toSection(page, site, data.go, 99) : undefined;

  // A place to go (even if the exact answer isn't on the site, the AI says where it's
  // most likely to be, and its answer explains that).
  if (best) {
    const where = onThisPage(page, best) ? '' : ` on ${pageName(best)}`;
    return {
      kind: 'found',
      headline: `I found ${best.noun}${where}.`,
      best: { section: best, confidence: data.found ? scoreToConfidence(data.score) : 'low' },
      others: [],
      answer: data.answer || undefined,
      sources,
    };
  }

  // An answer with nowhere to go, e.g. "summarize this page".
  if (data.answer && (data.found || data.mode === 'page')) {
    return { kind: 'found', headline: '', others: [], answer: data.answer, sources };
  }

  return {
    kind: 'none',
    topic: question.trim(),
    detail: site?.pages
      ? `Compass searched the ${site.pages} pages of ${page.domain} it has read so far.`
      : `Compass checked all ${page.sections.length} sections on this page.`,
    answer: data.answer || undefined,
    closest: sources[0] ? { section: sources[0], confidence: 'low' } : undefined,
    alternatives: [],
  };
}

/**
 * Answers a question: asks the backend, and if it can't be reached
 * (offline, asleep for too long, or an error), falls back to keyword matching.
 */
export async function askCompass(page: PageInfo, question: string, history: Turn[] = [], site: SiteInfo | null = null): Promise<QueryResult> {
  try {
    return fromServer(page, site, question, await askServer(page, question, history));
  } catch (err) {
    console.warn('[Compass] server unavailable, using keyword matching:', err);
    return { ...askLocally(page, question), offline: true };
  }
}

/* ------------------------------------------------------------------ */
/* Moving around                                                       */
/* ------------------------------------------------------------------ */

export type NavResult = 'arrived' | 'page' | 'stopped' | 'lost';

/** Steps shown on the Navigating screen when the section is on another page. */
export type NavStage = 'opening' | 'loading' | 'scrolling';

/**
 * Takes the person to a section.
 *  - On this page: the content script scrolls to it and highlights it.
 *  - On another page: the content script opens the link (through its dropdown menu if it
 *    has one), we wait for the new page to load, then scroll to the heading there.
 * The content script only answers once the scroll has finished, so meanwhile
 * `onProgress` creeps towards 90% to show something is happening.
 *
 * Returns 'page' when we reached the right page but couldn't find the heading on it.
 */
export async function navigateTo(
  page: PageInfo,
  section: Section,
  onProgress: (p: number) => void,
  signal: AbortSignal,
  onStage: (stage: NavStage, via?: string) => void = () => {},
): Promise<NavResult> {
  if (page.tabId === null) return 'lost';
  const tabId = page.tabId;

  let progress = 0;
  let ceiling = 0.9;
  const ticker = setInterval(() => {
    progress += (ceiling - progress) * 0.12;
    onProgress(progress);
  }, 80);

  try {
    if (!onThisPage(page, section)) {
      ceiling = 0.35;
      onStage('opening', section.menu);
      const reply = await messageTab<{ ok: boolean; via?: string }>(tabId, { type: 'OPEN_LINK', url: section.url, menu: section.menu }, 3000);
      if (signal.aborted) return 'stopped';
      if (reply?.ok) onStage('opening', reply.via || section.menu);
      else await browser.tabs.update(tabId, { url: section.url }); // no link on this page: just go there

      ceiling = 0.6;
      onStage('loading');
      const loaded = await waitForNewPage(tabId, page.url, section.url, signal);
      if (signal.aborted) return 'stopped';
      if (!loaded) return 'lost';
      if (section.level === 'page') {
        onProgress(1);
        await wait(300);
        return 'arrived';
      }
      ceiling = 0.9;
      onStage('scrolling');
    }

    // A whole page (no heading): just go to its top.
    const text = section.level === 'page' ? '' : section.title;
    const reply = await messageTab<{ ok: boolean }>(tabId, { type: 'GO_TO', id: section.id, text }, 8000);
    if (signal.aborted) return 'stopped';
    if (!reply?.ok) return onThisPage(page, section) ? 'lost' : 'page';
    onProgress(1);
    await wait(350); // let "Highlight on the page" tick before the next screen
    return signal.aborted ? 'stopped' : 'arrived';
  } catch {
    return 'lost';
  } finally {
    clearInterval(ticker);
  }
}

/** Scrolls the page back to where it was before the first "Go to section", and removes the highlight. */
export function goBack(page: PageInfo) {
  if (page.tabId !== null) browser.tabs.sendMessage(page.tabId, { type: 'BACK' }).catch(() => {});
}

/** Goes back to the previous page in the tab (after Compass opened another page). */
export function goBackPage(page: PageInfo) {
  if (page.tabId !== null) browser.tabs.goBack(page.tabId).catch(() => {});
}

/** A link that opens the page at this section. Uses the heading's id if it has one, otherwise a
 *  text fragment (#:~:text=…), which Chrome scrolls to and highlights by itself. */
export function sectionLink(page: PageInfo, section: Section): string {
  const base = onThisPage(page, section) ? (page.url.split('#')[0] ?? '') : section.url;
  if (section.level === 'page') return base;
  return base + (section.anchor || `#:~:text=${encodeURIComponent(section.title)}`);
}

/** Opens another page in the tab. */
export function openPage(url: string) {
  browser.tabs.update({ url }).catch(() => {});
}

/* ------------------------------------------------------------------ */
/* Recent questions, saved in chrome.storage.local                     */
/* ------------------------------------------------------------------ */

export interface RecentItem {
  question: string;
  domain: string;
  at: number;
}

export const recentStore = storage.defineItem<RecentItem[]>('local:recent', { fallback: [] });

export async function addRecent(item: RecentItem) {
  const list = await recentStore.getValue();
  const next = [item, ...list.filter((r) => !(r.question === item.question && r.domain === item.domain))].slice(0, 6);
  await recentStore.setValue(next);
}

/** 5 minutes ago -> "5m", yesterday -> "Yesterday", last week -> "Mon". */
export function timeAgo(at: number, now = Date.now()): string {
  const min = Math.floor((now - at) / 60000);
  if (min < 1) return 'Now';
  if (min < 60) return `${min}m`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours}h`;
  if (hours < 48) return 'Yesterday';
  return new Date(at).toLocaleDateString('en', { weekday: 'short' });
}
