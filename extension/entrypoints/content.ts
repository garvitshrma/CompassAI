// Runs inside every web page. It does nothing until the side panel sends it a message:
//   GET_PAGE  read the headings on the page
//   GO_TO     scroll to one heading and highlight it
//   BACK      scroll back to where the person was before GO_TO

export default defineContentScript({
  matches: ['<all_urls>'],
  main() {
    browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message.type === 'GET_PAGE') {
        sendResponse(readPage());
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

function readPage() {
  const pageHeight = Math.max(document.documentElement.scrollHeight, 1);

  const headings = contentHeadings().map((h, i) => {
    // Label the element so GO_TO can find it again later.
    // The side panel uses the same id ("h0", "h1", …) for its sections.
    h.dataset.compassId = `h${i}`;
    return {
      tag: h.tagName,
      text: clean(h.innerText),
      position: (h.getBoundingClientRect().top + window.scrollY) / pageHeight,
      summary: textAfter(h),
      anchor: h.id ? `#${h.id}` : '',
    };
  });

  return {
    url: location.href,
    title: document.title,
    screens: pageHeight / window.innerHeight,
    viewTop: window.scrollY / pageHeight,
    headings,
  };
}

/** Squash line breaks and repeated spaces into single spaces. */
function clean(text: string) {
  return text.replace(/\s+/g, ' ').trim();
}

/** The first ~200 characters after a heading, stopping at the next heading. */
function textAfter(heading: HTMLElement): string {
  let text = '';
  let el = heading.nextElementSibling ?? heading.parentElement?.nextElementSibling ?? null;
  while (el && text.length < 200) {
    if (el.matches(HEADINGS) || el.querySelector(HEADINGS)) break;
    text += ' ' + (el as HTMLElement).innerText;
    el = el.nextElementSibling;
  }
  const short = clean(text);
  return short.length > 200 ? short.slice(0, 197) + '…' : short;
}

/* ------------------------------------------------------------------ */
/* Finding a heading again                                             */
/* ------------------------------------------------------------------ */

/**
 * Two ways, best first:
 *  1. the data-compass-id label we put on it in readPage() — if its text still matches
 *  2. any content heading with exactly that text — in case the site re-drew the page since
 */
function findHeading(id: string, text: string): HTMLElement | null {
  const labelled = document.querySelector<HTMLElement>(`[data-compass-id="${CSS.escape(id)}"]`);
  if (labelled && clean(labelled.innerText) === text) return labelled;
  return contentHeadings().find((h) => clean(h.innerText) === text) ?? null;
}

/* ------------------------------------------------------------------ */
/* Scrolling                                                           */
/* ------------------------------------------------------------------ */

let returnTo: number | null = null; // scroll position before the first GO_TO

async function goTo(id: string, text: string) {
  const heading = findHeading(id, text);
  if (!heading) return { ok: false };

  if (returnTo === null) returnTo = window.scrollY;

  // Leave a gap above the heading so a sticky site header doesn't cover it.
  const gap = Math.min(120, window.innerHeight * 0.15);
  const top = heading.getBoundingClientRect().top + window.scrollY - gap;
  await smoothScroll(top);

  highlight(heading);
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

function highlight(heading: HTMLElement) {
  removeHighlight();
  target = heading;

  host = document.createElement('compass-highlight');
  host.style.cssText = 'position:absolute;top:0;left:0;width:0;height:0;z-index:2147483647;';
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `<style>${HIGHLIGHT_CSS}</style><div class="box"><span class="tag">Compass</span></div>`;
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
