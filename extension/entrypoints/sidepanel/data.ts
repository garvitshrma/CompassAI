// Everything the side panel knows about "the page" and "the answer" comes through this file.
//
//   - analyzePage  REAL: asks the content script in the active tab for its headings (see tab.ts)
//   - askCompass   REAL: sends the question + this page's headings to the backend's /page-query,
//                  falls back to local keyword matching if the server can't be reached
//   - navigateTo   REAL: tells the content script to scroll to the heading and highlight it
// The screens only depend on the types below, so they shouldn't need to change.

import type { SuggestionIcon } from './icons';
import { activeTabUrl, readActiveTab, type TabRead } from './tab';
import { ANSWER_TIMEOUT_MS, API_URL } from './config';

export type Confidence = 'high' | 'medium' | 'low';

export interface Section {
  id: string;
  level: string;         // "H1" | "H2" | "H3"
  number: string;        // "§7"
  group: string;         // "Billing"
  title: string;         // "Refunds and Cancellations"
  noun: string;          // "the refund policy"  -> "I found the refund policy."
  summary: string;
  points: string[];      // shown under "In this section"
  anchor: string;        // "#refunds" — the real version will be a CSS selector
  position: number;      // 0 = top of the page, 1 = bottom
}

export interface PageLink {
  label: string;         // "Open Pricing and search there"
  url: string;
  keywords: string[];
  why: string;           // "Discounts are usually listed on a pricing page."
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
  links: PageLink[];
  suggestions: Suggestion[];
}

export interface Match {
  section: Section;
  confidence: Confidence;
}

export type QueryResult =
  | {
      kind: 'found';
      headline: string;
      best: Match;
      others: Match[];
      explanation?: string; // why this section answers it (from the server's AI)
      offline?: boolean;    // true = the server couldn't be reached; this is keyword matching
    }
  | {
      kind: 'none';
      topic: string;
      detail: string;
      link?: PageLink;
      closest?: Match;
      alternatives: string[];
      offline?: boolean;
    };

/* ------------------------------------------------------------------ */
/* Turning what the content script read into a PageInfo               */
/* ------------------------------------------------------------------ */

function splitUrl(url: string) {
  try {
    const u = new URL(url);
    if (!u.protocol.startsWith('http')) return { domain: url, path: '' };
    return { domain: u.hostname.replace(/^www\./, ''), path: u.pathname === '/' ? '' : u.pathname };
  } catch {
    return { domain: url || 'This page', path: '' };
  }
}

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
  page.sections = read.headings.map((h, i) => {
    // The group is the nearest heading above this one with a higher rank, e.g. the H2 above an H3.
    const parent = read.headings.slice(0, i).reverse().find((p) => rank(p.tag) < rank(h.tag));
    return {
      id: `h${i}`,
      level: h.tag,
      number: '', // real pages have no "§7" numbering
      group: parent?.text ?? read.title,
      title: h.text,
      noun: `“${h.text}”`,
      summary: h.summary,
      points: [],
      anchor: h.anchor,
      position: Math.min(Math.max(h.position, 0), 1),
    };
  });

  // "Try asking" = the page's main headings.
  const main = page.sections.filter((_, i) => read.headings[i]?.tag === 'H2');
  page.suggestions = (main.length ? main : page.sections).slice(0, 4).map((sec) => ({
    text: `Take me to ${sec.title}`,
    icon: 'hash' as const,
  }));
  return page;
}

/* ------------------------------------------------------------------ */
/* Text helpers                                                        */
/* ------------------------------------------------------------------ */

const STOP = new Set(
  'a an and are can do does find for from get go how i in is it me my of on or page please show take that the there this to what where which who why with you your about any'.split(' '),
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

/** Instant, local heading filter used while the person types (screen 06 · Query). */
export function matchHeadings(page: PageInfo, text: string): { word: string; sections: Section[] } {
  const words = keywords(text);
  for (const w of [...words].sort((a, b) => b.length - a.length)) {
    const sections = page.sections.filter((sec) => has(sec.title, w));
    if (sections.length) return { word: w, sections: sections.slice(0, 5) };
  }
  return { word: '', sections: [] };
}

/** How far a section is from a point on the page, in words: "About 3 screens down". */
export function distance(page: PageInfo, from: number, to: number): string {
  const screens = Math.round((to - from) * page.screens);
  if (screens === 0) return 'On this screen';
  const n = Math.abs(screens);
  return `${n} screen${n === 1 ? '' : 's'} ${screens > 0 ? 'down' : 'up'}`;
}

/** Joins the non-empty parts: label('§7', 'Billing') -> "§7 · Billing", label('', 'Billing') -> "Billing". */
export const label = (...parts: string[]) => parts.filter(Boolean).join(' · ');

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ */
/* The three calls the real extension will replace                    */
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
  const link = page.links.find((l) => l.keywords.some((k) => words.includes(k)));
  const closestSection = best?.section ?? page.sections.find((sec) => link?.keywords.some((k) => has(sec.title, k)));
  return {
    kind: 'none',
    topic,
    detail: `Compass checked all ${page.sections.length} sections.${link ? ' ' + link.why : ''}`,
    link,
    closest: closestSection ? { section: closestSection, confidence: 'low' } : undefined,
    alternatives: link?.alternatives ?? [],
  };
}

/* ------------------------------------------------------------------ */
/* Asking the backend                                                  */
/* ------------------------------------------------------------------ */

/** What the backend's /page-query sends back (see page_query.py). */
interface ServerAnswer {
  found: boolean;
  id: string | null;
  explanation: string | null;
  reason: string | null;
  matches: { id: string; score: number }[];
}

/** Only scheme://host/path leaves the browser: query strings can hold session ids or personal data. */
function shareableUrl(url: string) {
  try {
    const u = new URL(url);
    return u.origin + u.pathname;
  } catch {
    return '';
  }
}

const scoreToConfidence = (score: number): Confidence => (score >= 0.4 ? 'high' : score >= 0.28 ? 'medium' : 'low');

async function askServer(page: PageInfo, question: string): Promise<ServerAnswer> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ANSWER_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_URL}/page-query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: ctrl.signal,
      body: JSON.stringify({
        question,
        url: shareableUrl(page.url),
        title: page.title,
        // Only the headings and the short text under them, never the whole page.
        sections: page.sections.map((s) => ({ id: s.id, tag: s.level, heading: s.title, summary: s.summary })),
      }),
    });
    if (!res.ok) throw new Error(`server answered ${res.status}`);
    const data = (await res.json()) as ServerAnswer;
    if (data.reason === 'the assistant is temporarily unavailable') throw new Error(data.reason);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

/** Turns the server's answer into the shape the screens use. */
function fromServer(page: PageInfo, question: string, data: ServerAnswer): QueryResult {
  const byId = (id: string | null) => page.sections.find((s) => s.id === id);
  const scoreOf = (id: string) => data.matches.find((m) => m.id === id)?.score ?? 0;
  const best = data.found ? byId(data.id) : undefined;

  if (best) {
    const others = data.matches
      .filter((m) => m.id !== best.id && m.score >= 0.2)
      .slice(0, 3)
      .map((m) => byId(m.id))
      .filter((s): s is Section => !!s)
      .map((section) => ({ section, confidence: scoreToConfidence(scoreOf(section.id)) }));
    return {
      kind: 'found',
      headline: `I found ${best.noun}.`,
      best: { section: best, confidence: scoreToConfidence(scoreOf(best.id)) },
      others,
      explanation: data.explanation ?? undefined,
    };
  }

  const top = data.matches[0];
  const closest = top && top.score >= 0.15 ? byId(top.id) : undefined;
  return {
    kind: 'none',
    topic: keywords(question).join(' ') || question.trim(),
    detail: `Compass checked all ${page.sections.length} sections on this page.`,
    closest: closest ? { section: closest, confidence: 'low' } : undefined,
    alternatives: [],
  };
}

/**
 * Answers a question about the page: asks the backend, and if it can't be reached
 * (offline, asleep for too long, or an error), falls back to keyword matching.
 */
export async function askCompass(page: PageInfo, question: string): Promise<QueryResult> {
  if (page.sections.length === 0) return askLocally(page, question);
  try {
    return fromServer(page, question, await askServer(page, question));
  } catch (err) {
    console.warn('[Compass] server unavailable, using keyword matching:', err);
    return { ...askLocally(page, question), offline: true };
  }
}

export type NavResult = 'arrived' | 'stopped' | 'lost';

/**
 * Asks the content script to scroll to a section and highlight it.
 * The content script only answers once the scroll has finished, so meanwhile
 * `onProgress` creeps towards 90% to show something is happening.
 */
export async function navigateTo(page: PageInfo, section: Section, onProgress: (p: number) => void, signal: AbortSignal): Promise<NavResult> {
  if (page.tabId === null) return 'lost';

  let progress = 0;
  const ticker = setInterval(() => {
    progress += (0.9 - progress) * 0.15;
    onProgress(progress);
  }, 80);

  try {
    const reply = await browser.tabs.sendMessage(page.tabId, { type: 'GO_TO', id: section.id, text: section.title });
    if (signal.aborted) return 'stopped';
    if (!reply?.ok) return 'lost';
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

/** A link that opens the page at this section. Uses the heading's id if it has one, otherwise a
 *  text fragment (#:~:text=…), which Chrome scrolls to and highlights by itself. */
export function sectionLink(page: PageInfo, section: Section): string {
  const base = page.url.split('#')[0];
  return base + (section.anchor || `#:~:text=${encodeURIComponent(section.title)}`);
}

/** Opens another page in the tab. Mock: only logs, because the mock sites aren't real. */
export function openPage(url: string) {
  console.info('[Compass mock] would open', url);
  // Real version: browser.tabs.update({ url });
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
