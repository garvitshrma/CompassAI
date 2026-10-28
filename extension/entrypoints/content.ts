// Runs inside every web page. It does nothing until the side panel sends it a message:
//   GET_PAGE   read the headings on the page (and the text under each one)
//   GET_LINKS  list the site's navigation links, including the ones inside dropdown menus
//   OPEN_LINK  open another page the way a person would: open its dropdown, point at the link, click it
//   GO_TO      scroll to one heading and highlight it
//   BACK       scroll back to where the person was before GO_TO

export default defineContentScript({
  matches: ['<all_urls>'],
  main() {
    browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message.type === 'GET_PAGE') {
        sendResponse(readPage());
      } else if (message.type === 'GET_LINKS') {
        getLinks().then(sendResponse);
        return true;
      } else if (message.type === 'OPEN_LINK') {
        openLink(message.url, message.menu ?? '').then(sendResponse);
        return true;
      } else if (message.type === 'GO_TO') {
        goTo(message.id, message.text).then(sendResponse);
        return true; // "I'll answer later": keeps sendResponse alive until the scroll finishes
      } else if (message.type === 'BACK') {
        goBack();
        sendResponse({ ok: true });
      }
    });
    window.addEventListener('resize', placeHighlight);
  },
});

/* ------------------------------------------------------------------ */
/* Reading the page                                                    */
/* ------------------------------------------------------------------ */

// Headings inside these are site navigation, not content.
// Same idea as in_chrome() in chunker.py.
const SITE_CHROME = 'nav, header, footer, [role="navigation"], [role="banner"], [role="contentinfo"]';
const HEADINGS = 'h1, h2, h3';

/** The real content headings on the page, in order. */
function contentHeadings() {
  return [...document.querySelectorAll<HTMLElement>(HEADINGS)]
    .filter((h) => !h.closest(SITE_CHROME)) // skip navbar / footer headings
    .filter((h) => h.getClientRects().length > 0) // skip hidden headings (display: none)
    .filter((h) => clean(h.innerText).length > 0);
}

const SECTION_CHARS = 1200; // text kept under each heading
const SKIP_TEXT = 'script, style, noscript, template, svg, select, textarea, input, button';

/**
 * The visible text under each heading, up to the next heading, in reading order.
 * Walks every text node once. Unlike "the next few sibling elements", this also works
 * when a heading sits deep inside wrapper <div>s, which most real sites (apple.com…) do.
 * Text before the first heading is returned as `intro`.
 */
function sectionTexts(headings: HTMLElement[]) {
  const index = new Map(headings.map((h, i) => [h, i]));
  const texts = headings.map(() => '');
  let intro = '';
  let current = -1;
  const visible = new WeakMap<Element, boolean>();
  const isShown = (el: Element) => {
    let v = visible.get(el);
    if (v === undefined) {
      v = el.checkVisibility ? el.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true }) : el.getClientRects().length > 0;
      visible.set(el, v);
    }
    return v;
  };

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        const el = node as Element;
        if (index.has(el as HTMLElement)) return NodeFilter.FILTER_ACCEPT;
        if (el.matches(SKIP_TEXT) || el.matches(SITE_CHROME)) return NodeFilter.FILTER_REJECT; // skip the whole subtree
        return NodeFilter.FILTER_SKIP;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      current = index.get(node as HTMLElement)!;
      // Skip the heading's own text.
      const after = (node as Element).lastChild ? lastDescendant(node) : node;
      walker.currentNode = after;
      continue;
    }
    const parent = node.parentElement;
    const t = node.textContent ?? '';
    if (!parent || !t.trim() || !isShown(parent)) continue;
    if (current < 0) {
      if (intro.length < 800) intro += ' ' + t;
    } else if (texts[current]!.length < SECTION_CHARS) {
      texts[current] += ' ' + t;
    }
  }
  return { texts: texts.map((t) => shorten(clean(t), SECTION_CHARS)), intro: shorten(clean(intro), 800) };
}

function lastDescendant(node: Node): Node {
  let n = node;
  while (n.lastChild) n = n.lastChild;
  return n;
}

function readPage() {
  const pageHeight = Math.max(document.documentElement.scrollHeight, 1);
  const found = contentHeadings();
  const { texts, intro } = sectionTexts(found);

  const headings = found.map((h, i) => {
    // Label the element so GO_TO can find it again later.
    // The side panel uses the same id ("h0", "h1", …) for its sections.
    h.dataset.compassId = `h${i}`;
    return {
      tag: h.tagName,
      text: clean(h.innerText),
      position: (h.getBoundingClientRect().top + window.scrollY) / pageHeight,
      summary: shorten(texts[i] ?? '', 200),
      body: texts[i] ?? '', // sent to the server only when the person asks a question
      anchor: h.id ? `#${h.id}` : '',
    };
  });

  const description = document.querySelector<HTMLMetaElement>('meta[name="description"], meta[property="og:description"]')?.content ?? '';

  return {
    url: location.href,
    title: document.title,
    screens: pageHeight / window.innerHeight,
    viewTop: window.scrollY / pageHeight,
    headings,
    // For "summarize this page", and for pages with few or no headings.
    intro: shorten(clean(`${description} ${intro}`), 1000),
    text: headings.length < 3 ? shorten(clean((document.querySelector('main') ?? document.body).innerText), 6000) : '',
  };
}

/** Squash line breaks and repeated spaces into single spaces. */
function clean(text: string) {
  return text.replace(/\s+/g, ' ').trim();
}

const shorten = (text: string, max: number) => (text.length > max ? text.slice(0, max - 1) + '…' : text);

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ */
/* Finding a heading again                                             */
/* ------------------------------------------------------------------ */

const words = (text: string) => new Set(text.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? []);
const squash = (text: string) => text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

/** How alike two headings are, 0–1. Exact (ignoring spaces and punctuation) = 1. */
function similarity(a: string, b: string): number {
  if (!a || !b) return 0;
  if (squash(a) === squash(b)) return 1;
  const A = words(a);
  const B = words(b);
  if (!A.size || !B.size) return 0;
  let common = 0;
  A.forEach((w) => B.has(w) && common++);
  return (2 * common) / (A.size + B.size); // Dice coefficient: 1 = same words
}

/**
 * Finds the heading to go to. The heading text can come from our server's copy of the
 * page, which differs a little from the live page (hidden words, line breaks, emoji), so:
 *  1. the data-compass-id label we put on it in readPage(), if its text still matches
 *  2. otherwise the heading whose words are most alike (at least 75% alike)
 * Hidden headings count too (inside a closed tab, modal or accordion): goTo() reveals them.
 */
function findHeading(id: string, text: string): HTMLElement | null {
  if (id) {
    const labelled = document.querySelector<HTMLElement>(`[data-compass-id="${CSS.escape(id)}"]`);
    if (labelled && similarity(clean(labelled.innerText), text) === 1) return labelled;
  }
  let best: HTMLElement | null = null;
  let bestScore = 0;
  for (const h of document.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6, [role="heading"]')) {
    // innerText is what's visible; textContent includes hidden words the server also saw.
    let score = Math.max(similarity(clean(h.innerText), text), similarity(clean(h.textContent ?? ''), text));
    if (!shown(h)) score -= 0.05; // prefer a visible copy
    if (h.closest(SITE_CHROME)) score -= 0.1; // and one that isn't in the menus
    if (score > bestScore) {
      bestScore = score;
      best = h;
    }
  }
  return bestScore >= 0.75 ? best : null;
}

/** Like findHeading, but keeps looking for a few seconds: on sites built with React and
 *  friends, the content appears a moment after the page itself has loaded. */
async function waitForHeading(id: string, text: string, ms = 4000) {
  const until = Date.now() + ms;
  let heading = findHeading(id, text);
  while (!heading && Date.now() < until) {
    await wait(250);
    heading = findHeading(id, text);
  }
  return heading;
}

/** Really visible: laid out, and not hidden with visibility or opacity. */
const shown = (el: Element) =>
  el.checkVisibility ? el.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true }) : el.getClientRects().length > 0;

const OPENERS = 'button, a, summary, [role="button"], [role="tab"]';

/** A visible button/link/tab that points at an element by id (aria-controls, href="#…", data-*). */
function openerFor(id: string): HTMLElement | null {
  for (const el of document.querySelectorAll<HTMLElement>(OPENERS)) {
    if (el.closest('form') || !shown(el)) continue;
    for (const attr of el.attributes) {
      const v = attr.value.trim();
      if ((v === id || v === `#${id}`) && (attr.name === 'href' || attr.name.startsWith('aria-') || attr.name.startsWith('data-')))
        return el;
    }
  }
  return null;
}

/**
 * A heading can exist but be hidden: in a closed accordion (<details>), a tab that
 * isn't selected, or a pop-up (apple.com's "More about M5" panels). Try to open
 * whatever hides it, like a person would: open the <details>, or click the button
 * that points at the hidden part.
 */
async function reveal(heading: HTMLElement) {
  for (let el: HTMLElement | null = heading; el && el !== document.body; el = el.parentElement) {
    if (shown(heading)) return;
    if (el instanceof HTMLDetailsElement && !el.open) {
      el.open = true;
      await wait(150);
      continue;
    }
    if (!el.id) continue;
    const opener = openerFor(el.id);
    if (opener) {
      opener.click(); // opens the tab / accordion / pop-up that holds the heading
      await wait(600);
    }
  }
}

/** The nearest ancestor that's big enough to see, but not most of the page. */
function sizable(el: HTMLElement): HTMLElement | null {
  for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
    const r = a.getBoundingClientRect();
    if (r.width > 40 && r.height > 16 && shown(a)) return r.height < window.innerHeight * 1.5 ? a : null;
  }
  return null;
}

/** The nearest visible part of the page around a heading we couldn't reveal (a carousel
 *  slide, say) — as long as it's not most of the page. */
function visibleAround(heading: HTMLElement): HTMLElement | null {
  for (let el = heading.parentElement; el && el !== document.body; el = el.parentElement) {
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    return r.height > 0 && r.height < window.innerHeight * 1.5 ? el : null;
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* The site's navigation, including dropdown menus                     */
/* ------------------------------------------------------------------ */

// Many sites hide part of their navigation in dropdown menus. Two kinds:
//  1. CSS menus: the links are in the page, just invisible until you hover.
//     We can read them straight away.
//  2. JavaScript menus: the links don't exist at all until the menu is opened.
//     For these we open each menu (hover, then click if needed), collect what
//     appeared, and close it again. That's why the menus flicker briefly when
//     the panel opens. We only touch elements that say they open a menu
//     (aria-haspopup / aria-expanded / <summary>) inside the site's navigation.

const MENU_AREAS = 'nav, header, [role="navigation"], [role="banner"], [role="menubar"]';
const TRIGGERS = '[aria-haspopup]:not([aria-haspopup="false"]), [aria-expanded="false"], summary';
const MAX_TRIGGERS = 12;

interface NavLink {
  url: string; // origin + path, no query or #fragment
  text: string; // "Summer of Code"
  menu: string; // the dropdown it sits in, e.g. "Programs" ("" = not in a dropdown)
}

const siteOf = (host: string) => host.toLowerCase().replace(/^www\./, '');

/** The link's address if it points to another page of this website, else ''. */
function sameSiteUrl(href: string | null): string {
  if (!href) return '';
  try {
    const u = new URL(href, location.href);
    if (!/^https?:$/.test(u.protocol) || siteOf(u.hostname) !== siteOf(location.hostname)) return '';
    return u.origin + u.pathname.replace(/\/$/, '');
  } catch {
    return '';
  }
}

/** "Programs ▾" -> "Programs" */
const labelOf = (el: Element) =>
  clean(el.textContent || el.getAttribute('aria-label') || '').replace(/[▾▼▿⌄⌃›»↓+]/g, '').trim().slice(0, 60);

const isVisible = (el: Element) => {
  if (el.getClientRects().length === 0) return false;
  const style = getComputedStyle(el);
  return style.visibility !== 'hidden' && Number(style.opacity) > 0.05;
};

/** Is `el` something that opens a menu (and safe to click: not a real link, not a form button)? */
function isTrigger(el: Element): el is HTMLElement {
  if (!(el instanceof HTMLElement) || el.closest('form')) return false;
  if (el instanceof HTMLAnchorElement && !/^(#|javascript:|$)/i.test(el.getAttribute('href') ?? '')) return false;
  if (el.getAttribute('type') === 'submit') return false; // (a <button> outside a form is harmless)
  return true;
}

/** Which dropdown a link sits in: the menu's trigger element and its label. */
function menuOf(a: Element): { label: string; trigger: HTMLElement | null; container: HTMLElement | null } {
  let el = a.parentElement;
  for (let depth = 0; el && depth < 6 && !el.matches('nav, header, body'); depth++, el = el.parentElement) {
    if (el.id) {
      const t = document.querySelector<HTMLElement>(`[aria-controls="${CSS.escape(el.id)}"]`);
      if (t && !t.contains(a)) return { label: labelOf(t), trigger: t, container: el };
    }
    if (el instanceof HTMLDetailsElement) {
      const summary = el.querySelector<HTMLElement>(':scope > summary');
      if (summary) return { label: labelOf(summary), trigger: summary, container: el };
    }
    const prev = el.previousElementSibling;
    const looksLikeTrigger =
      prev &&
      !prev.querySelector('a[href]') &&
      (prev.matches('button, summary, [role="button"], [aria-haspopup], [aria-expanded]') || !isVisible(el)) &&
      el.querySelectorAll('a[href]').length >= 2;
    if (looksLikeTrigger && labelOf(prev).length > 0 && labelOf(prev).length <= 40)
      return { label: labelOf(prev), trigger: prev as HTMLElement, container: el };
  }
  return { label: '', trigger: null, container: null };
}

function hover(el: HTMLElement, on: boolean) {
  for (const target of [el, el.parentElement]) {
    if (!target) continue;
    for (const type of on ? ['pointerover', 'pointerenter', 'mouseover', 'mouseenter'] : ['pointerout', 'pointerleave', 'mouseout', 'mouseleave'])
      target.dispatchEvent(new MouseEvent(type, { bubbles: !type.endsWith('enter') && !type.endsWith('leave'), view: window }));
  }
}

const isOpen = (t: HTMLElement) =>
  t.getAttribute('aria-expanded') === 'true' || (t.tagName === 'SUMMARY' && !!(t.parentElement as HTMLDetailsElement | null)?.open);

/** Opens a menu: hover first (many menus open on hover), click if that did nothing. */
async function openMenu(trigger: HTMLElement, appeared: () => boolean): Promise<boolean> {
  hover(trigger, true);
  await wait(150);
  if (appeared()) return false;
  if (!isOpen(trigger) && trigger.matches(TRIGGERS)) {
    trigger.click();
    await wait(300);
    return true; // we clicked, so we must click again to close it
  }
  return false;
}

let linksCache: { href: string; links: NavLink[] } | null = null;

async function getLinks(): Promise<NavLink[]> {
  if (linksCache?.href === location.href) return linksCache.links;
  const found = new Map<string, NavLink>();
  const add = (a: HTMLAnchorElement, menu?: string) => {
    const url = sameSiteUrl(a.getAttribute('href'));
    const text = labelOf(a);
    if (url && text && !found.has(url)) found.set(url, { url, text, menu: menu ?? menuOf(a).label });
  };

  // 1. Links already in the navigation, visible or hidden in a CSS dropdown.
  document.querySelectorAll<HTMLAnchorElement>(`:is(${MENU_AREAS}) a[href]`).forEach((a) => add(a));

  // 2. JavaScript menus: open each one and collect the links that appear.
  const triggers = [...new Set(document.querySelectorAll(`:is(${MENU_AREAS}) :is(${TRIGGERS})`))]
    .filter(isTrigger)
    .filter(isVisible)
    .slice(0, MAX_TRIGGERS);
  for (const trigger of triggers) {
    const before = new Set(document.querySelectorAll('a[href]'));
    const fresh = () => [...document.querySelectorAll<HTMLAnchorElement>('a[href]')].filter((a) => !before.has(a));
    try {
      const clicked = await openMenu(trigger, () => fresh().length > 0);
      fresh().forEach((a) => add(a, labelOf(trigger)));
      if (clicked && isOpen(trigger)) trigger.click(); // close it again
      hover(trigger, false);
    } catch {
      // One stubborn menu shouldn't stop the others.
    }
  }

  const links = [...found.values()].slice(0, 80);
  linksCache = { href: location.href, links };
  return links;
}

/** A link on this page to `url`, preferring a visible one. */
function findLink(url: string): HTMLAnchorElement | null {
  const all = [...document.querySelectorAll<HTMLAnchorElement>('a[href]')].filter((a) => sameSiteUrl(a.getAttribute('href')) === url);
  return all.find(isVisible) ?? all.find((a) => a.closest(MENU_AREAS)) ?? all[0] ?? null;
}

/** Shows a hidden CSS menu by force (synthetic hovers can't trigger CSS :hover). */
function forceShow(a: Element) {
  const touched: HTMLElement[] = [];
  for (let el = a.parentElement; el && !el.matches('nav, header, body'); el = el.parentElement) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) < 0.05) {
      el.style.setProperty('display', style.display === 'none' ? 'block' : style.display, 'important');
      el.style.setProperty('visibility', 'visible', 'important');
      el.style.setProperty('opacity', '1', 'important');
      touched.push(el);
    }
  }
  setTimeout(() => touched.forEach((el) => ['display', 'visibility', 'opacity'].forEach((p) => el.style.removeProperty(p))), 2500);
}

/**
 * Opens another page of the site like a person would: opens the dropdown the link
 * is in, points at the link, then clicks it. Answers first, because clicking
 * navigates away and this script (and its reply) would be gone.
 */
async function openLink(url: string, menu: string) {
  let a = findLink(url);
  let via = '';

  if (!a || !isVisible(a)) {
    // Find the menu: from the link itself, or (JS menus) from the label the panel gave us.
    let trigger = a ? menuOf(a).trigger : null;
    if (!trigger && menu) {
      const want = menu.toLowerCase();
      const named = [...document.querySelectorAll<HTMLElement>(`:is(${MENU_AREAS}) *`)].filter(
        (el) => isVisible(el) && labelOf(el).toLowerCase() === want,
      );
      // Prefer the element that says it opens a menu, else the innermost one with that label.
      trigger =
        named.find((el) => el.matches(TRIGGERS)) ??
        named.find((el) => ![...el.children].some((c) => labelOf(c).toLowerCase() === want)) ??
        null;
    }
    if (trigger && isTrigger(trigger)) {
      via = labelOf(trigger);
      await openMenu(trigger, () => {
        const l = findLink(url);
        return !!l && isVisible(l);
      });
      a = findLink(url);
    }
    if (a && !isVisible(a)) forceShow(a);
  }

  if (!a || !isVisible(a)) return { ok: false };

  const link = a;
  highlight(link, via ? `${via} › ${labelOf(link)}` : labelOf(link) || 'Opening');
  setTimeout(() => {
    removeHighlight();
    if (link.target === '_blank') location.assign(link.href);
    else link.click();
  }, 1200);
  return { ok: true, via };
}

/* ------------------------------------------------------------------ */
/* Scrolling                                                           */
/* ------------------------------------------------------------------ */

let returnTo: number | null = null; // scroll position before the first GO_TO

async function goTo(id: string, text: string) {
  if (!text) {
    // The whole page was asked for: its top.
    if (returnTo === null) returnTo = window.scrollY;
    await smoothScroll(0);
    return { ok: true };
  }
  const heading = await waitForHeading(id, text);
  if (!heading) return { ok: false };

  let target: HTMLElement = heading;
  let tag = 'Compass';
  if (!shown(heading)) await reveal(heading);
  if (!shown(heading)) {
    // Still hidden (a carousel slide, say): take them to the part of the page that holds it.
    const around = visibleAround(heading);
    if (!around) return { ok: false };
    target = around;
    tag = `“${shorten(clean(heading.textContent ?? ''), 40)}” is in here`;
  }

  // Some headings are only for screen readers (1×1 pixel). Point at what's around them.
  const box = target.getBoundingClientRect();
  if (box.width < 4 || box.height < 4) target = sizable(target) ?? target;

  if (returnTo === null) returnTo = window.scrollY;

  // A heading inside a pop-up is already on screen; otherwise scroll to it, leaving a
  // gap above so a sticky site header doesn't cover it.
  if (!target.closest('[role="dialog"], [aria-modal="true"], dialog')) {
    const gap = Math.min(120, window.innerHeight * 0.15);
    const top = target.getBoundingClientRect().top + window.scrollY - gap;
    await smoothScroll(top);
  }

  highlight(target, tag);
  return { ok: true };
}

function goBack() {
  removeHighlight();
  if (returnTo !== null) window.scrollTo({ top: returnTo, behavior: 'smooth' });
  returnTo = null;
}

/** Scrolls smoothly and waits until the scrolling has stopped. */
function smoothScroll(top: number) {
  return new Promise<void>((resolve) => {
    const target = Math.max(0, Math.min(top, document.documentElement.scrollHeight - window.innerHeight));
    if (Math.abs(window.scrollY - target) < 2) return resolve();
    const done = () => {
      window.removeEventListener('scrollend', done);
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(done, 2000); // in case 'scrollend' never fires
    window.addEventListener('scrollend', done);
    window.scrollTo({ top: target, behavior: 'smooth' });
  });
}

/* ------------------------------------------------------------------ */
/* Highlighting                                                        */
/* ------------------------------------------------------------------ */

// Our highlight is drawn inside a Shadow DOM: a private mini-document attached to one
// element. The site's CSS can't reach into it, and our CSS can't leak out and break the site.

let host: HTMLElement | null = null;
let box: HTMLElement | null = null;
let target: HTMLElement | null = null;
let fadeTimer: ReturnType<typeof setTimeout> | undefined;

const HIGHLIGHT_CSS = `
  .box {
    position: absolute;
    border: 2px solid #17160F;
    border-radius: 10px;
    background: transparent;          /* sits on top of the page, so no fill: the text stays crisp */
    box-shadow: 0 0 0 4px rgba(255, 253, 248, 0.9), 0 8px 24px -8px rgba(23, 22, 15, 0.45);
    pointer-events: none;            /* clicks go straight through to the page */
    animation: in 0.35s cubic-bezier(.2, .9, .3, 1.2) both, pulse 1.6s ease-in-out 0.35s 2;
    transition: opacity 0.6s ease;
  }
  .box.gone { opacity: 0; }
  .tag {
    position: absolute; top: -26px; left: -2px;
    padding: 3px 8px; border-radius: 6px;
    background: #17160F; color: #FFFDF8;
    font: 600 11.5px/1.4 ui-sans-serif, system-ui, sans-serif;
    white-space: nowrap;
  }
  @keyframes in { from { transform: scale(0.96); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes pulse { 50% { box-shadow: 0 0 0 10px rgba(255, 253, 248, 0.6), 0 8px 24px -8px rgba(23, 22, 15, 0.45); } }
  @media (prefers-reduced-motion: reduce) { .box { animation: none; } }
`;

function highlight(heading: HTMLElement, tag = 'Compass') {
  removeHighlight();
  target = heading;

  host = document.createElement('compass-highlight');
  host.style.cssText = 'position:absolute;top:0;left:0;width:0;height:0;z-index:2147483647;';
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `<style>${HIGHLIGHT_CSS}</style><div class="box"><span class="tag"></span></div>`;
  shadow.querySelector('.tag')!.textContent = tag;
  box = shadow.querySelector('.box');
  document.documentElement.append(host);
  placeHighlight();

  // Fade out after a while so it doesn't sit on the page forever.
  fadeTimer = setTimeout(() => {
    box?.classList.add('gone');
    fadeTimer = setTimeout(removeHighlight, 700);
  }, 6000);
}

/** Puts the box around the heading, in page coordinates so it scrolls with the page. */
function placeHighlight() {
  if (!box || !target) return;
  // A Range around the heading's text gives the size of the words themselves,
  // not the full-width block the heading sits in.
  const range = document.createRange();
  range.selectNodeContents(target);
  const r = range.getBoundingClientRect().width > 0 ? range.getBoundingClientRect() : target.getBoundingClientRect();
  const pad = 8;
  Object.assign(box.style, {
    top: `${r.top + window.scrollY - pad}px`,
    left: `${r.left + window.scrollX - pad}px`,
    width: `${r.width + pad * 2}px`,
    height: `${r.height + pad * 2}px`,
  });
}

function removeHighlight() {
  clearTimeout(fadeTimer);
  host?.remove();
  host = box = target = null;
}
