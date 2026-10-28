/* ===========================================================================
 *  compass-widget.js — the Compass widget. One file, no dependencies, no build.
 * ===========================================================================
 *
 *  THE IDEA: A DEPTH SOUNDER
 *  -------------------------
 *  Closed, it is a sounding disc: concentric depth rings with a live core.
 *  Asking drops a ping — one ripple leaves the core — and the answer surfaces
 *  from below. Question and answer, call and echo.
 *
 *  It is not decoration chosen at random. A sonar reading is the one everyday
 *  object whose whole purpose is "send a question into something you cannot
 *  see, and get back a location" — which is literally what this does: you ask,
 *  and it tells you WHERE on the site the answer lives. On openlake.in it also
 *  happens to rhyme with the name, which is a bonus rather than the reason.
 *
 *  Everything else is kept quiet so the ping is the only thing that moves.
 *
 *  IT WEARS THE HOST SITE'S COLOURS
 *  --------------------------------
 *  Every value below is `var(--token, fallback)`. CSS CUSTOM PROPERTIES
 *  INHERIT THROUGH A SHADOW BOUNDARY — unlike ordinary styles, which cannot
 *  reach in — so a site that defines --background, --surface, --brand-sky and
 *  the rest gets a widget in its own palette with no configuration, and one
 *  that FOLLOWS A LIGHT/DARK TOGGLE automatically, because the site redefines
 *  those variables and the widget simply inherits the new values.
 *
 *  The fallback after each comma is what a site WITHOUT those tokens gets: a
 *  deep slate panel with a cyan accent. Legible anywhere, and `data-accent`
 *  overrides the one colour that carries the brand.
 *
 *  WHY VANILLA JS AND A SHADOW ROOT
 *  --------------------------------
 *  It is a <script> on someone else's page. No bundle, no build step, no React
 *  version to clash with. A shadow root is a separate style scope, so the host
 *  page's resets cannot touch the widget and the widget cannot leak onto them.
 *
 *  USAGE
 *      <script src="compass-widget.js" data-api="https://your-api.example"></script>
 *
 *  ATTRIBUTES
 *      data-api          API base URL
 *      data-site         site_id to query  (default location.hostname)
 *      data-name         title in the header
 *      data-accent       the one brand colour
 *      data-suggestions  pipe-separated starter questions
 *      data-side         "left" or "right"  (default "right")
 *
 *  The host element carries id="compass-bot" so the page can hide or move the
 *  whole widget with one CSS rule without reaching into the shadow root.
 * ======================================================================== */

(function () {
  "use strict";

  // document.currentScript is the <script> executing right now — how we read
  // our own data- attributes without needing an id. Valid ONLY during initial
  // synchronous execution; read it in a callback and it is null.
  const me = document.currentScript;
  const d = (k, fb) => (me && me.dataset[k]) || fb;

  const API = d("api", "https://compassai-fm0k.onrender.com");
  // location.hostname is what makes one script tag work on any site: the
  // server normalises it (strips www, port, case) into a site_id.
  const SITE = d("site", location.hostname);
  const NAME = d("name", "Ask this site");
  const ACCENT = d("accent", "var(--brand-sky, var(--accent-ink, #28a9e2))");
  const LEFT = d("side", "right") === "left";
  const SUGGESTIONS = d("suggestions", "How do I join?|What projects are there?|Who do I contact?")
    .split("|").map(s => s.trim()).filter(Boolean).slice(0, 3);

  // ---- mount -------------------------------------------------------------
  const host = document.createElement("div");
  host.id = "compass-bot";
  // 2147483647 is the max 32-bit signed int — the conventional "above the host
  // page's sticky headers and modals" value. Only the anchored side is set, so
  // a page overriding the position in CSS has nothing to fight.
  host.style.cssText =
    "position:fixed;z-index:2147483647;bottom:22px;" +
    (LEFT ? "left:22px;" : "right:22px;");
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  root.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; }

      /* ---- tokens -----------------------------------------------------
         Read once here, used everywhere below, so the mapping from the host
         site's names to this widget's roles is in ONE place. Re-point these
         and the whole widget re-skins. */
      :host {
        --w-bg:      var(--surface, #0d141a);
        --w-deep:    var(--deep-950, #0c1c2b);
        --w-page:    var(--background, #05080b);
        --w-fg:      var(--foreground, #ffffff);
        --w-muted:   var(--muted, #ffffffb8);
        --w-line:    var(--border, #ffffff2e);
        --w-grid:    var(--grid-line, #ffffff1a);
        --w-accent:  ${ACCENT};
        --w-accent-2: var(--sky-300, #7cc9ee);
        --w-ui:      var(--font-phantom, ui-sans-serif, system-ui, sans-serif);
        --w-display: var(--font-zarathustra, var(--font-phantom, Georgia, serif));
        --w-shadow:  var(--shadow-dd, 0 8px 32px #0009);
      }

      /* ================= CLOSED: the sounding disc ===================== */
      .fab {
        position: relative; display: grid; place-items: center;
        width: 60px; height: 60px; padding: 0;
        border: 1px solid var(--w-line);
        border-radius: 999px;
        background:
          radial-gradient(circle at 50% 50%,
            color-mix(in srgb, var(--w-accent) 18%, transparent) 0%,
            transparent 62%),
          var(--w-deep);
        box-shadow: var(--w-shadow);
        cursor: pointer;
        transition: transform .18s cubic-bezier(.2,.8,.3,1), border-color .18s;
      }
      .fab:hover { transform: translateY(-3px); border-color: var(--w-accent); }
      .fab:focus-visible { outline: 2px solid var(--w-accent); outline-offset: 4px; }
      .fab svg { display: block; width: 34px; height: 34px; }

      /* The ping. ONE ripple, on hover and while thinking — never idle. A
         widget that pulses forever on someone else's page is a distraction
         they did not ask for. */
      .ring { transform-origin: 50% 50%; opacity: 0; }
      @media (prefers-reduced-motion: no-preference) {
        .fab:hover .ring { animation: ping 1.6s ease-out infinite; }
        .fab:hover .ring:nth-child(2) { animation-delay: .5s; }
      }
      @keyframes ping {
        0%   { transform: scale(.35); opacity: .9; }
        100% { transform: scale(1.25); opacity: 0; }
      }

      /* ================= OPEN: the panel =============================== */
      .panel {
        display: none; width: 352px; max-width: calc(100vw - 32px);
        border: 1px solid var(--w-line);
        border-radius: 16px;
        background: var(--w-bg);
        box-shadow: var(--w-shadow);
        overflow: hidden;
        font-family: var(--w-ui);
        color: var(--w-fg);
      }
      .panel.open { display: block; }
      @media (prefers-reduced-motion: no-preference) {
        .panel.open { animation: surface .22s cubic-bezier(.2,.8,.3,1) both; }
      }
      /* It rises, because the answer comes from below. */
      @keyframes surface {
        from { transform: translateY(10px); opacity: 0; }
        to   { transform: translateY(0);    opacity: 1; }
      }

      header {
        display: flex; align-items: center; gap: 10px;
        padding: 13px 14px 12px;
        background: linear-gradient(to bottom,
          color-mix(in srgb, var(--w-accent) 9%, var(--w-deep)), var(--w-deep));
      }
      .core {
        position: relative; width: 9px; height: 9px; border-radius: 999px;
        background: var(--w-accent); flex: none;
        box-shadow: 0 0 10px color-mix(in srgb, var(--w-accent) 70%, transparent);
      }
      /* The core pings while a question is in flight — motion that reports
         state, which is the only kind worth having here. */
      .core::after {
        content: ""; position: absolute; inset: -4px;
        border: 1px solid var(--w-accent); border-radius: 999px; opacity: 0;
      }
      .thinking .core::after { animation: ping 1.1s ease-out infinite; }

      /* The site's own face. An earlier draft set this in --font-zarathustra,
         the serif openlake.in loads — but a look at the live page showed
         phantomSans on every one of its 34 visible text elements and the serif
         on none. A second family the site never shows is not "matching the
         site", it is a guest rearranging the furniture. */
      .title {
        font-family: var(--w-ui); font-size: 15.5px; font-weight: 600;
        letter-spacing: -.01em; flex: 1;
      }
      .shut {
        background: none; border: 0; cursor: pointer; padding: 2px 4px;
        color: var(--w-muted); font-size: 18px; line-height: 1;
        border-radius: 6px;
      }
      .shut:hover { color: var(--w-fg); background: var(--w-line); }
      .shut:focus-visible { outline: 2px solid var(--w-accent); outline-offset: 1px; }

      /* The waterline: the surface you are asking through. */
      .line {
        height: 1px;
        background: linear-gradient(to right,
          transparent, var(--w-accent), var(--w-accent-2), transparent);
        opacity: .65;
      }

      /* ---- the body ---------------------------------------------------
         A faint grid, the way a depth chart is ruled. Low enough that it
         reads as texture and never competes with the text on top of it. */
      .body {
        position: relative;
        padding: 15px 15px 6px;
        min-height: 92px;
        font-size: 14.5px; line-height: 1.6;
        background-image:
          linear-gradient(var(--w-grid) 1px, transparent 1px),
          linear-gradient(90deg, var(--w-grid) 1px, transparent 1px);
        background-size: 26px 26px;
        background-position: -1px -1px;
      }
      .body > * { position: relative; }
      @media (prefers-reduced-motion: no-preference) {
        .answer { animation: surface .26s cubic-bezier(.2,.8,.3,1) both; }
      }

      .muted { color: var(--w-muted); }
      /* WHICH TOPIC MATCHED. It was a tracked-out uppercase label above the
         answer, which is the single most generic treatment available and also
         shouts louder than the answer it introduces. It is now a contact
         marker: a lit dot and the heading as written. Same information, and it
         belongs to the sounding idea rather than to every landing page ever. */
      .heading {
        display: flex; align-items: center; gap: 8px;
        font-size: 13px; color: var(--w-accent-2);
        margin-bottom: 7px;
      }
      .heading::before {
        content: ""; flex: none;
        width: 5px; height: 5px; border-radius: 999px;
        background: var(--w-accent);
        box-shadow: 0 0 0 3px color-mix(in srgb, var(--w-accent) 20%, transparent);
      }
      .text a {
        color: var(--w-accent-2); font-weight: 600;
        text-underline-offset: 3px;
      }
      .go {
        display: inline-block; margin-top: 10px;
        padding: 7px 12px; border-radius: 8px;
        border: 1px solid color-mix(in srgb, var(--w-accent) 55%, transparent);
        color: var(--w-accent-2); font-size: 13.5px; font-weight: 600;
        text-decoration: none;
      }
      .go:hover { background: color-mix(in srgb, var(--w-accent) 14%, transparent); }

      /* ---- starter questions -----------------------------------------
         An empty chat box asks the visitor to guess what it knows. Three real
         questions answer that, and they are the ones most people want anyway —
         so this is navigation, not decoration. They vanish once used. */
      .chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 2px 15px 12px; }
      .chip {
        font-family: var(--w-ui); font-size: 12.5px;
        padding: 6px 11px; border-radius: 999px; cursor: pointer;
        color: var(--w-muted); background: transparent;
        border: 1px solid var(--w-line);
        transition: color .15s, border-color .15s;
      }
      .chip:hover { color: var(--w-fg); border-color: var(--w-accent); }
      .chip:focus-visible { outline: 2px solid var(--w-accent); outline-offset: 1px; }

      /* ---- the ask row ------------------------------------------------ */
      form {
        display: flex; gap: 8px; align-items: center;
        padding: 11px 12px 12px;
        border-top: 1px solid var(--w-line);
        background: var(--w-deep);
      }
      input {
        flex: 1; min-width: 0;
        font-family: var(--w-ui); font-size: 14px;
        padding: 9px 12px; border-radius: 9px;
        color: var(--w-fg);
        background: var(--w-page);
        border: 1px solid var(--w-line);
      }
      input::placeholder { color: var(--w-muted); opacity: .75; }
      input:focus { outline: none; border-color: var(--w-accent); }

      .send {
        display: grid; place-items: center; flex: none;
        width: 38px; height: 38px; padding: 0;
        border: 0; border-radius: 9px; cursor: pointer;
        background: var(--w-accent);
        color: var(--w-page);
        transition: transform .14s, opacity .14s;
      }
      .send:hover { transform: translateY(-1px); }
      .send:disabled { opacity: .45; cursor: default; transform: none; }
      .send:focus-visible { outline: 2px solid var(--w-accent-2); outline-offset: 2px; }
      .send svg { width: 17px; height: 17px; }

      ::selection { background: var(--w-accent); color: var(--w-page); }

      @media (prefers-reduced-motion: reduce) {
        .fab, .send, .chip { transition: none; }
      }
    </style>

    <div class="panel" part="panel">
      <header>
        <span class="core" aria-hidden="true"></span>
        <span class="title">${NAME}</span>
        <button class="shut" type="button" aria-label="Close">&times;</button>
      </header>
      <div class="line" aria-hidden="true"></div>
      <div class="body">
        <div class="answer muted">Ask about this site and I'll point you to the right place.</div>
      </div>
      <div class="chips">
        ${SUGGESTIONS.map(s => `<button class="chip" type="button">${s.replace(/[<>&"]/g, "")}</button>`).join("")}
      </div>
      <form>
        <input type="text" placeholder="Ask a question" autocomplete="off" aria-label="Your question" />
        <button class="send" type="submit" aria-label="Send">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
               stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 12h13M12 5l7 7-7 7"/>
          </svg>
        </button>
      </form>
    </div>

    <button class="fab" type="button" aria-label="${NAME}">
      <svg viewBox="0 0 40 40" aria-hidden="true">
        <!-- depth rings: the chart you read a sounding off -->
        <circle cx="20" cy="20" r="15" fill="none" stroke="var(--w-line)" stroke-width="1"/>
        <circle cx="20" cy="20" r="10" fill="none" stroke="var(--w-line)" stroke-width="1"/>
        <!-- the ping, on hover only -->
        <circle class="ring" cx="20" cy="20" r="12" fill="none"
                stroke="var(--w-accent)" stroke-width="1.5"/>
        <circle class="ring" cx="20" cy="20" r="12" fill="none"
                stroke="var(--w-accent)" stroke-width="1.5"/>
        <!-- the return: one contact, off-centre, the way a real reading sits -->
        <circle cx="26" cy="15" r="2.8" fill="var(--w-accent-2)"/>
        <circle cx="20" cy="20" r="3.6" fill="var(--w-accent)"/>
      </svg>
    </button>
  `;

  const fab = root.querySelector(".fab");
  const panel = root.querySelector(".panel");
  const body = root.querySelector(".body");
  const answer = root.querySelector(".answer");
  const chips = root.querySelector(".chips");
  const form = root.querySelector("form");
  const input = root.querySelector("input");
  const sendBtn = root.querySelector(".send");

  function open(on) {
    panel.classList.toggle("open", on);
    fab.style.display = on ? "none" : "grid";
    if (on) input.focus();
  }
  fab.addEventListener("click", () => open(true));
  root.querySelector(".shut").addEventListener("click", () => { open(false); fab.focus(); });
  root.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && panel.classList.contains("open")) { open(false); fab.focus(); }
  });

  chips.addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    input.value = b.textContent;
    ask(b.textContent);
  });

  // escapeHtml IS NOT OPTIONAL. The explanation comes from an LLM reading text
  // from a web page, and it goes into innerHTML. Without escaping, a stray
  // "<img onerror=...>" in the indexed content would execute as script on the
  // host's page. Always escape anything you did not author into innerHTML.
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function say(html, muted) {
    answer.className = "answer" + (muted ? " muted" : "");
    answer.innerHTML = html;
  }

  // ======================================================================
  //  highlight() — THE PAYOFF FUNCTION. Unchanged in behaviour; only the
  //  outline colour now follows the accent instead of being hardcoded.
  // ======================================================================
  function highlight(selector) {
    let el;
    try {
      el = document.querySelector(selector);
    } catch (e) {
      // querySelector THROWS a SyntaxError on a malformed selector — it does
      // not return null. An empty string, or an id starting with a digit, will
      // throw. Without this catch one odd chunk takes down the whole widget.
      return false;
    }
    // A valid selector matching nothing is the EXPECTED outcome when the site
    // was redesigned after indexing, which is why the caller checks the return
    // value and shows a softer message instead of pretending it scrolled.
    if (!el) return false;

    // block:"center" rather than the default "start", which would tuck the
    // target under a sticky header where nobody would notice it.
    el.scrollIntoView({ behavior: "smooth", block: "center" });

    // LEAVE NO TRACE: save every property before touching it and put it all
    // back, or we permanently strip an outline the site's own design needed.
    const prevOutline = el.style.outline;
    const prevOffset = el.style.outlineOffset;
    const prevTransition = el.style.transition;

    // Transition set FIRST so the colour change animates rather than snapping.
    el.style.transition = "outline-color .3s";
    // outline, not border: a border occupies space in the box model and would
    // shift the page as it appears.
    el.style.outline = "3px solid " + getComputedStyle(host).getPropertyValue("--w-accent").trim();
    el.style.outlineOffset = "4px";
    setTimeout(() => { el.style.outline = "3px solid transparent"; }, 1800);
    setTimeout(() => {
      el.style.outline = prevOutline;
      el.style.outlineOffset = prevOffset;
      el.style.transition = prevTransition;
    }, 2200);
    return true;
  }

  async function ask(q) {
    chips.style.display = "none";          // they have served their purpose
    panel.classList.add("thinking");        // the core starts pinging
    say("Sounding…", true);
    sendBtn.disabled = true;
    try {
      const res = await fetch(API + "/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, site_id: SITE }),
      });
      const data = await res.json();

      if (!data.found) {
        say(escapeHtml(data.reason || "I don't have anything on that."), true);
        return;
      }

      const head = `<div class="heading">${escapeHtml(data.heading)}</div>`;
      const text = `<div class="text">${escapeHtml(data.explanation)}</div>`;

      // Normalise both sides before comparing: strip a trailing slash and drop
      // any fragment, so "/about/" and "/about#team" both equal "/about".
      // Without this a same-page answer reads as a different page and the
      // visitor gets a pointless link to where they already are.
      const samePage = !data.url ||
        data.url.replace(/\/$/, "") === location.href.replace(/\/$/, "").split("#")[0];

      if (samePage) {
        // ANSWER-ONLY CHUNKS: a fact typed by a human has no selector, because
        // it was never on a page. There is nothing to scroll to and nothing
        // wrong, so say nothing about locating anything. Without this branch
        // querySelector(null) coerces to the string "null", matches nothing,
        // and a correct answer ends up apologising.
        if (!data.selector) { say(head + text, false); return; }

        const ok = highlight(data.selector);
        say(head + text + (ok
          ? `<div class="muted" style="margin-top:8px">Taking you there</div>`
          : `<div class="muted" style="margin-top:8px">Couldn't find it on this page</div>`), false);
      } else {
        // CROSS-PAGE: clicking through destroys this script and runs it fresh
        // on the new page with no memory. sessionStorage survives navigation
        // within the tab; the block at the bottom of this file picks it up.
        // Scoped to the tab, not localStorage, so a stale target cannot make a
        // random future visit scroll somewhere unexpected days later.
        if (data.selector) sessionStorage.setItem("compass_target", data.selector);
        say(head + text +
          `<a class="go" href="${escapeHtml(data.url)}">Take me there</a>`, false);
      }
    } catch (e) {
      // Network failure, CORS rejection, invalid JSON, or the server asleep.
      // Says what happened and what to do, in the interface's voice.
      say("Can't reach the assistant right now. Try again in a moment.", true);
    } finally {
      // `finally` runs on every path including the early returns, so the
      // button is never left permanently disabled.
      panel.classList.remove("thinking");
      sendBtn.disabled = false;
      input.select();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();                     // never reload the host page
    const q = input.value.trim();
    if (q) ask(q);
  });

  // ---- the second half of the cross-page handoff -------------------------
  const pending = sessionStorage.getItem("compass_target");
  if (pending && pending !== "null") {
    sessionStorage.removeItem("compass_target");
    // After load, so the target element exists and images have settled enough
    // for scrollIntoView to land in the right place.
    addEventListener("load", () => setTimeout(() => highlight(pending), 450));
  } else if (pending) {
    sessionStorage.removeItem("compass_target");
  }
})();
