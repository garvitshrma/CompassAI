/*
 * ============================================================================
 *  compass-widget.js — the embeddable Compass widget (STAGE 6, the browser)
 * ============================================================================
 *
 * Loads as a single <script> tag. Injects a floating button + chat panel
 * into a Shadow DOM (so the host site's CSS can't touch it and vice versa).
 * On a question, calls the Compass API, then scrolls to and highlights the
 * returned element.
 *
 * ---------------------------------------------------------------------------
 *  THIS IS WHERE THE PRODUCT ACTUALLY BECOMES DIFFERENT
 * ---------------------------------------------------------------------------
 *  Everything else in this repo exists to produce one pair of values:
 *
 *      { url, selector }
 *
 *  This file is what turns that pair into the visible behaviour a competitor
 *  cannot copy without rebuilding their whole index: `document.querySelector`,
 *  `scrollIntoView`, and a green outline on the real element. The visitor
 *  verifies the answer with their own eyes instead of trusting a paragraph of
 *  generated text.
 *
 * ---------------------------------------------------------------------------
 *  THE THREE HARD CONSTRAINTS OF WRITING AN EMBEDDABLE WIDGET
 * ---------------------------------------------------------------------------
 *  You are a guest running inside someone else's page. Therefore:
 *
 *   1. YOU MUST NOT BREAK THEIR SITE. No global variables, no CSS that leaks
 *      out, no permanent changes to their DOM. (Solved by the IIFE + Shadow DOM
 *      + the style-restoration in highlight().)
 *
 *   2. THEIR SITE MUST NOT BREAK YOU. Their CSS reset, their `* { margin: 0 }`,
 *      their z-index wars. (Solved by Shadow DOM + `all: initial`.)
 *
 *   3. YOU MUST SURVIVE A PAGE NAVIGATION. Clicking a link destroys this entire
 *      script and reloads it fresh, with no memory. (Solved by sessionStorage.)
 *
 *  read more:
 *    Shadow DOM ........ https://developer.mozilla.org/en-US/docs/Web/API/Web_components/Using_shadow_DOM
 *    querySelector ..... https://developer.mozilla.org/en-US/docs/Web/API/Document/querySelector
 *    scrollIntoView .... https://developer.mozilla.org/en-US/docs/Web/API/Element/scrollIntoView
 *    fetch() ........... https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch
 *    sessionStorage .... https://developer.mozilla.org/en-US/docs/Web/API/Window/sessionStorage
 *
 * Local test:
 *   add  <script src="compass-widget.js"></script>  to a saved page,
 *   serve the folder with:  python -m http.server 5500
 *   open  http://localhost:5500/index.html
 *
 * Config via data-attributes on the script tag:
 *   data-api   API base URL (default http://localhost:8000)
 */

// THE IIFE — "Immediately Invoked Function Expression".
// Everything is wrapped in a function that is defined and called at once. The
// reason is scope: any `const`/`let`/`function` declared at the top level of a
// classic script becomes GLOBAL, shared with the host page. If the host site
// happened to have its own variable called `panel` or `input`, one of us would
// silently clobber the other and something would break in a way that is very
// hard to diagnose. Inside a function, our names are ours alone.
// read more: https://developer.mozilla.org/en-US/docs/Glossary/IIFE
(function () {
  // "use strict" opts into stricter JS semantics: assigning to an undeclared
  // variable throws instead of silently creating a global, duplicate parameter
  // names are errors, and several legacy footguns are disabled.
  // read more: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Strict_mode
  "use strict";

  // ---- find our own <script> tag to read config ----
  // `document.currentScript` returns the <script> element that is executing
  // RIGHT NOW. That lets a site owner configure the widget purely in HTML:
  //
  //     <script src="compass-widget.js"
  //             data-api="https://my-api.com"
  //             data-site="example.com"></script>
  //
  // CRITICAL DETAIL: currentScript is only valid during INITIAL synchronous
  // execution. Read it inside a setTimeout or a callback and you get null. That
  // is exactly why this is the first line of the file.
  // read more: https://developer.mozilla.org/en-US/docs/Web/API/Document/currentScript
  const me = document.currentScript;

  // `.dataset.api` maps to the data-api attribute (dataset camel-cases the name
  // after "data-"). The `||` chain provides a default, and the `me &&` guard
  // handles currentScript being null in odd loading situations — without it,
  // reading `.dataset` of null would throw and the widget would never load.
  const API = (me && me.dataset.api) || "https://compassai-za69.onrender.com";

  // The site identity. Defaulting to `location.hostname` is what makes
  // installation zero-config: the widget works out which index it belongs to
  // from the page it is running on. The server then runs this through
  // normalize_site_id() so "www." and casing do not matter.
  const SITE = (me && me.dataset.site) || location.hostname;

  // ---- create an isolated host element ----
  const host = document.createElement("div");
  host.id = "compass-widget-host";

  // `all: initial` resets EVERY CSS property on this element to its default,
  // which severs any inherited styling from the host page — their font, colour,
  // line-height, box-sizing, and so on. This is belt-and-braces alongside the
  // Shadow DOM below (a few properties, notably inherited ones, can otherwise
  // still cross the shadow boundary).
  // read more: https://developer.mozilla.org/en-US/docs/Web/CSS/all
  host.style.all = "initial";            // stop host page inheritance

  document.body.appendChild(host);

  // ======================================================================
  //  SHADOW DOM — the single most important line for an embeddable widget
  // ======================================================================
  // attachShadow creates a separate, encapsulated DOM tree hanging off this
  // element. It gives us a true isolation boundary in BOTH directions:
  //
  //   * The host page's CSS cannot reach inside. Their `button { background:
  //     red !important }` does not touch our button. Their CSS reset does not
  //     flatten our panel.
  //   * Our CSS cannot leak out. Our `.panel { ... }` rule cannot accidentally
  //     restyle an element of theirs that happens to share the class name.
  //
  // Without this you would have to write defensive CSS with absurd specificity
  // and hope — which is what widget developers did for a decade before Shadow
  // DOM existed, and it never fully worked.
  //
  // mode: "open" means external JS can still reach in via `host.shadowRoot`.
  // "closed" would hide it, but that is security theatre (any script on the page
  // could monkey-patch attachShadow anyway) and it makes debugging much harder.
  // read more: https://developer.mozilla.org/en-US/docs/Web/API/Element/attachShadow
  const root = host.attachShadow({ mode: "open" });

  // ---- everything inside the shadow root ----
  // The entire UI — styles and markup — is defined in one template literal and
  // injected at once. For a widget of this size that is simpler and faster than
  // building elements one createElement at a time, and keeping the CSS adjacent
  // to the markup it styles is genuinely easier to maintain.
  // read more: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Template_literals
  root.innerHTML = `
    <style>
      /* :host targets the shadow root's host element itself (our outer div).
         Repeating the reset here covers styles applied via CSS rather than the
         inline style property set above.
         read more: https://developer.mozilla.org/en-US/docs/Web/CSS/:host */
      :host { all: initial; }

      /* A universal selector is normally bad practice, but INSIDE a shadow root
         it is completely safe — its reach stops at the boundary, so it can
         never touch the host page. We use it to establish our own baseline,
         since we inherit nothing.
         box-sizing: border-box makes width include padding and border, which is
         what everyone actually wants.
         The font stack uses system-ui first so the widget looks native on every
         OS without downloading a font file (fast, and no external request that
         a strict Content-Security-Policy might block). */
      * { box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; }

      /* FAB = "Floating Action Button", the little circle in the corner. */
      .fab {
        /* position: fixed pins it to the VIEWPORT, so it stays put as the page
           scrolls — essential, since the widget must remain reachable while the
           visitor is reading anywhere on the page. */
        position: fixed; bottom: 24px; right: 24px; width: 56px; height: 56px;

        /* 50% radius on a square = a perfect circle. */
        border-radius: 50%; border: none; cursor: pointer;

        /* 2147483647 is the maximum 32-bit signed integer, i.e. the highest
           z-index that exists. Necessary because we have no idea what the host
           site stacks on top of what — their sticky header might be at 9999.
           This guarantees we are never buried.
           read more: https://developer.mozilla.org/en-US/docs/Web/CSS/z-index */
        z-index: 2147483647;

        background: #1f6f5c; color: #fff; font-size: 24px;
        box-shadow: 0 4px 16px rgba(0,0,0,.25);

        /* flex centring is the reliable way to put the emoji dead-centre
           regardless of its font metrics. */
        display: flex; align-items: center; justify-content: center;

        /* Only the transform property is transitioned. Transform and opacity
           are the two
           properties browsers can animate on the GPU without recalculating
           layout, so this stays smooth on a slow device.
           read more: https://web.dev/articles/animations-guide */
        transition: transform .15s;
      }
      .fab:hover { transform: scale(1.06); }

      .panel {
        position: fixed; bottom: 92px; right: 24px; width: 340px;

        /* The responsive guard: on a narrow phone a fixed 340px would overflow
           the screen. calc(100vw - 48px) caps it at the viewport width minus
           our 24px margins, and max-width means the smaller of the two wins.
           read more: https://developer.mozilla.org/en-US/docs/Web/CSS/calc */
        max-width: calc(100vw - 48px);

        background: #fff; border-radius: 14px; z-index: 2147483647;
        box-shadow: 0 8px 32px rgba(0,0,0,.22);

        /* display:none hides the panel initially. Note flex-direction is
           declared here even while hidden, so that adding .open (which sets
           display:flex) immediately produces the right layout. */
        display: none; flex-direction: column; overflow: hidden;
        border: 1px solid #e5e7eb;
      }
      /* Toggling one class is the whole open/close mechanism. Keeping state in
         a CSS class rather than in a JS variable means the DOM is the single
         source of truth — see the toggle handler below. */
      .panel.open { display: flex; }

      .head {
        background: #1f6f5c; color: #fff; padding: 14px 16px;
        font-weight: 600; font-size: 15px;
      }
      .head small { display: block; font-weight: 400; opacity: .8; font-size: 12px; margin-top: 2px; }

      /* min-height stops the panel from visibly jumping in size as the answer
         text changes length — a small detail that makes it feel less janky. */
      .body { padding: 14px 16px; min-height: 60px; font-size: 14px; color: #1a1a1a; line-height: 1.5; }
      .body .muted { color: #6b7280; }
      .body a { color: #1f6f5c; }

      .inputRow { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid #eee; }
      /* flex: 1 makes the input absorb all leftover width, so the Go button
         keeps its natural size and the input grows to fill the rest. */
      .inputRow input {
        flex: 1; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 8px;
        font-size: 14px; outline: none;
      }
      /* We removed the default focus outline above, so we MUST provide our own
         focus indication — otherwise keyboard users cannot see where they are.
         Accessibility, not decoration. */
      .inputRow input:focus { border-color: #1f6f5c; }
      .inputRow button {
        border: none; background: #1f6f5c; color: #fff; border-radius: 8px;
        padding: 0 14px; cursor: pointer; font-size: 14px;
      }
      /* Visual feedback for the disabled state used while a request is in
         flight (see ask()), so the user can see why clicking does nothing. */
      .inputRow button:disabled { opacity: .5; cursor: default; }

      .foot { text-align: center; font-size: 11px; color: #9ca3af; padding: 6px; }
    </style>

    <button class="fab" title="Ask Compass">🧭</button>

    <div class="panel">
      <div class="head">
        Ask Compass
        <small>Tell me what you're looking for</small>
      </div>
      <div class="body">
        <!-- Example queries as placeholder text. This is real UX work, not
             filler: users faced with an empty box do not know what a new tool
             will understand. Showing two concrete examples teaches the mental
             model ("ask where things are") in one glance. -->
        <span class="muted">Try: "where are the projects" or "learn game development"</span>
      </div>
      <div class="inputRow">
        <input type="text" placeholder="Ask a question..." />
        <button class="send">Go</button>
      </div>
      <div class="foot">powered by Compass</div>
    </div>
  `;

  // Cache references to the elements we will touch repeatedly. Note we query
  // `root`, NOT `document` — these elements live inside the shadow tree and are
  // deliberately invisible to `document.querySelector`. (That invisibility is
  // the encapsulation working as designed.)
  const fab = root.querySelector(".fab");
  const panel = root.querySelector(".panel");
  const input = root.querySelector("input");
  const sendBtn = root.querySelector(".send");
  const body = root.querySelector(".body");

  // ---- open / close ----
  fab.addEventListener("click", () => {
    // classList.toggle adds the class if absent, removes it if present. The
    // open/closed state therefore lives entirely in the DOM — there is no
    // separate `let isOpen = false` variable that could drift out of sync with
    // what is actually on screen.
    // read more: https://developer.mozilla.org/en-US/docs/Web/API/Element/classList
    panel.classList.toggle("open");

    // Auto-focus the input on open so the visitor can start typing straight
    // away without a second click. Only when OPENING — focusing an element
    // inside a panel we just hid would be wrong (and would confuse a screen
    // reader).
    if (panel.classList.contains("open")) input.focus();
  });

  // ======================================================================
  //  highlight() — THE PAYOFF FUNCTION.
  //  Everything in this repository exists so that this function can be called
  //  with a selector that resolves.
  // ======================================================================
  function highlight(selector) {
    let el;
    try {
      // Run the selector our Python css_path() generated, months earlier, on a
      // different machine, against the live DOM.
      el = document.querySelector(selector);
    } catch (e) {
      // THE try/catch IS NECESSARY, not defensive padding. querySelector THROWS
      // a SyntaxError on a malformed selector — it does not return null. An
      // empty string, or a selector containing an id that starts with a digit,
      // will throw. Without this catch, one odd chunk would take down the whole
      // widget with an uncaught exception.
      return false;                      // malformed selector
    }

    // The other failure mode: a perfectly valid selector that matches nothing.
    // This is the EXPECTED outcome when the site has been redesigned since we
    // indexed it, and it is why the caller checks the return value and shows a
    // softer message rather than pretending the scroll worked.
    if (!el) return false;

    // THE SCROLL. behavior:"smooth" animates rather than teleporting, which is
    // what makes it read as "being taken somewhere" instead of the page simply
    // being different. block:"center" puts the target in the middle of the
    // viewport — better than the default "start", which would tuck it right
    // under a sticky header where the visitor might not notice it.
    // read more: https://developer.mozilla.org/en-US/docs/Web/API/Element/scrollIntoView
    el.scrollIntoView({ behavior: "smooth", block: "center" });

    // ------------------------------------------------------------------
    //  LEAVE NO TRACE. We are about to modify an element on someone else's
    //  page, so we save every property we are about to touch and put it all
    //  back afterwards. Skipping this would mean permanently stripping a
    //  transition or an outline the site's own design depended on.
    // ------------------------------------------------------------------
    // inject a keyframe + outline into the HOST page (not shadow) so it
    // wraps the real element. Scoped by a unique attribute so we clean up.
    const prevOutline = el.style.outline;
    const prevOffset = el.style.outlineOffset;
    const prevTransition = el.style.transition;

    // Set the transition FIRST, so the colour change below animates rather than
    // snapping. Order matters here.
    el.style.transition = "outline-color .3s";

    // WHY `outline` AND NOT `border`: a border occupies space in the box model,
    // so adding one would shift the element and everything around it — the page
    // would visibly jump at the exact moment the user is looking at it. An
    // outline is drawn outside the box and affects no layout whatsoever.
    // read more: https://developer.mozilla.org/en-US/docs/Web/CSS/outline
    el.style.outline = "3px solid #1f6f5c";

    // A few pixels of breathing room so the ring frames the text rather than
    // touching it.
    el.style.outlineOffset = "4px";

    // ---- the two-stage fade-out ----
    // pulse: fade the outline out after a moment
    //
    // 1800ms: switch the colour to transparent. Because of the transition set
    // above, this FADES over 0.3s instead of vanishing. The outline is still
    // technically present, just invisible.
    setTimeout(() => {
      el.style.outline = "3px solid transparent";
    }, 1800);

    // 2200ms (= 1800 + 300 fade + a small buffer): now that the fade has
    // finished, restore the original values. Doing this at 1800 instead would
    // cut the animation off mid-way. The 400ms gap is the animation duration
    // plus slack.
    setTimeout(() => {
      el.style.outline = prevOutline;
      el.style.outlineOffset = prevOffset;
      el.style.transition = prevTransition;
    }, 2200);

    return true;                          // caller uses this to pick a message
  }

  // ---- ask the API ----
  // `async` lets us use `await` and write the network call as straight-line
  // code instead of nested .then() callbacks.
  // read more: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Using_promises
  async function ask(q) {
    // Immediate feedback. A network round trip to Render (which may be waking
    // from a cold start) can take seconds; without this the widget looks frozen
    // and the user clicks again.
    body.innerHTML = `<span class="muted">Looking...</span>`;

    // Disable the button so an impatient double-click cannot fire two requests.
    sendBtn.disabled = true;

    try {
      const res = await fetch(API + "/query", {
        method: "POST",
        // WITHOUT A TIMEOUT this hangs indefinitely. On a sleeping free-tier
        // instance the first request can take ~50s to wake the container, and
        // the visitor stares at "Looking..." with no way to tell whether it is
        // slow or dead. 60s is chosen to sit just past a realistic cold start,
        // so a genuine wake-up still succeeds but a black hole does not hang
        // the widget forever.
        // read more: https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal/timeout_static
        signal: AbortSignal.timeout(60000),
        // This header is what makes the browser send a CORS PREFLIGHT — an
        // automatic OPTIONS request asking the server for permission first.
        // That is why "OPTIONS" appears in allow_methods in api.py; omit it
        // there and this request fails before it is ever sent.
        headers: { "Content-Type": "application/json" },
        // fetch cannot send an object; the body must be a string.
        body: JSON.stringify({ query: q, site_id: SITE }),
      });
      // ---- CHECK THE STATUS BEFORE PARSING ----
      // fetch does NOT reject on an HTTP error status — a 502 is a perfectly
      // successful fetch as far as the promise is concerned. So res.json() was
      // being handed Render's HTML error page, throwing a SyntaxError, and
      // landing in the catch below as the same generic "something went wrong"
      // that a real network failure produces.
      //
      // That cost real debugging time: a 502 caused by the server being
      // OOM-killed carries no CORS headers (it never reaches the app), so the
      // browser reports it as a CORS violation, and the widget reported it as
      // a generic failure. Three different stories for one root cause. Naming
      // the 5xx case explicitly is what makes the next outage readable.
      if (!res.ok) {
        body.innerHTML = res.status >= 500
          ? `<span class="muted">Compass is waking up or busy. Try again in a moment.</span>`
          : `<span class="muted">Compass couldn't handle that request (${res.status}).</span>`;
        return;
      }

      const data = await res.json();

      // ---- the refusal path ----
      // Remember api.py returns HTTP 200 even for "not found" — the outcome is
      // in the BODY, not the status code. So we branch on data.found, and the
      // widget never needs to inspect status codes at all.
      if (!data.found) {
        // Two genuinely different situations deserve two different messages:
        //   * the site was never indexed -> the SITE OWNER needs to act
        //   * nothing matched            -> the VISITOR should rephrase
        // Showing "try rephrasing" to someone on an unindexed site would send
        // them on a pointless loop.
        //
        // The `(data.reason || "")` guard handles reason being undefined, since
        // calling .includes() on undefined would throw.
        // NOTE: this string-matching couples the widget to api.py's exact
        // wording. A `code` field would be more robust; worth knowing as a
        // deliberate shortcut.
        const notRegistered = (data.reason || "").includes("not registered");
        body.innerHTML = notRegistered
          ? `<span class="muted">Compass isn't set up for this site yet.</span>`
          : `<span class="muted">I couldn't find that on this page. Try rephrasing?</span>`;
        return;
      }

      // ---- is the answer on the page we are already looking at? ----
      // Both sides are normalised before comparing:
      //   .replace(/\/$/, "")  strips a trailing slash, so "/about/" == "/about"
      //   .split("#")[0]       drops any fragment, so "/about#team" == "/about"
      // Without both, a same-page answer would be misread as a different page
      // and the visitor would be shown a pointless link to where they already are.
      const samePage =
        !data.url || data.url.replace(/\/$/, "") === location.href.replace(/\/$/, "").split("#")[0];

      if (samePage) {
        // THE GOOD PATH: scroll and highlight, right now.
        const ok = highlight(data.selector);

        // Note the message depends on whether the highlight actually succeeded.
        // If the selector no longer resolves (the site changed since indexing)
        // we still show the explanation — partial value beats a dead end — but
        // we say honestly that we could not locate it, rather than claiming to
        // have scrolled somewhere we did not.
        body.innerHTML = ok
          ? `${escapeHtml(data.explanation)} <br><span class="muted">↑ taking you there</span>`
          : `${escapeHtml(data.explanation)} <br><span class="muted">(couldn't locate it on this page)</span>`;
      } else {
        // ================================================================
        //  CROSS-PAGE NAVIGATION — solving constraint #3 from the header.
        // ================================================================
        // The answer lives on a different page. When the visitor clicks through,
        // this entire script is destroyed and re-executed fresh on the new page
        // with no memory of anything — closures, variables, everything gone.
        //
        // So we write the target selector into sessionStorage, which SURVIVES
        // navigation within the same tab. The block at the very bottom of this
        // file picks it up on the next page load and completes the journey.
        //
        // sessionStorage rather than localStorage on purpose: it is scoped to
        // this tab and is cleared when the tab closes. A stale pending target
        // sitting in localStorage could make a random future visit scroll
        // somewhere unexpected days later.
        // read more: https://developer.mozilla.org/en-US/docs/Web/API/Web_Storage_API
        sessionStorage.setItem("compass_target", data.selector);
        body.innerHTML = `${escapeHtml(data.explanation)} <br><a href="${data.url}">Take me there →</a>`;
      }
    } catch (e) {
      // Now only reached for genuine transport failures — HTTP error statuses
      // are handled above. A timeout raises TimeoutError, which deserves its
      // own wording: "it is slow" and "it is broken" call for different
      // reactions from the visitor.
      body.innerHTML = e && e.name === "TimeoutError"
        ? `<span class="muted">Taking too long to respond. Try again in a moment.</span>`
        : `<span class="muted">Couldn't reach Compass. Check your connection and try again.</span>`;
    } finally {
      // `finally` runs on every path — success, refusal, exception, and even the
      // early `return` in the not-found branch. That guarantees the button is
      // never left permanently disabled, which would silently brick the widget.
      sendBtn.disabled = false;
    }
  }

  // ---- XSS DEFENCE ----
  // `data.explanation` is text an LLM generated from content scraped off a
  // third-party website. We insert it with innerHTML, which EXECUTES any HTML in
  // the string — so if a page contained "<img src=x onerror=alert(1)>" and that
  // survived into the explanation, we would be running attacker-supplied code on
  // our customer's site. That is a cross-site scripting hole.
  //
  // Escaping the four structural characters neutralises it: they become harmless
  // visible text instead of markup.
  //
  // (The cleaner fix is `el.textContent = str`, which never interprets markup at
  //  all. We use innerHTML here because the surrounding template needs real <br>
  //  and <span> tags — so we escape the untrusted part and keep the trusted part
  //  as markup.)
  // read more: https://owasp.org/www-community/attacks/xss/
  function escapeHtml(s) {
    // The regex /[&<>"]/g matches each dangerous character ("g" = every
    // occurrence, not just the first). The replacer function looks each one up
    // in a small object mapping it to its HTML entity.
    // `&` must be in the list and is handled correctly here because we replace
    // in a single pass — a naive sequence of .replace() calls would double-encode
    // it into "&amp;lt;".
    return (s || "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
    );
  }

  // ---- submit handlers ----
  function submit() {
    // .trim() so a stray space is not treated as a real question.
    const q = input.value.trim();
    if (q) ask(q);       // silently ignore an empty submit
  }
  sendBtn.addEventListener("click", submit);

  // Enter-to-send. Essential: nobody reaches for the mouse after typing a
  // question. "keydown" rather than the deprecated "keypress", and e.key ===
  // "Enter" rather than the deprecated numeric e.keyCode === 13.
  // read more: https://developer.mozilla.org/en-US/docs/Web/API/KeyboardEvent/key
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });

  // ======================================================================
  //  RESUME AFTER CROSS-PAGE NAVIGATION
  //  The second half of the sessionStorage handoff started in ask(). This runs
  //  on EVERY page load, and almost always finds nothing — but when the visitor
  //  has just clicked "Take me there →", this is what completes the journey.
  // ======================================================================
  const pending = sessionStorage.getItem("compass_target");
  if (pending) {
    // REMOVE IT IMMEDIATELY, before using it. If we removed it afterwards and
    // something below threw, the stale target would persist and hijack the next
    // page load too. Consume-then-act is the safe ordering for a one-shot token.
    sessionStorage.removeItem("compass_target");

    // wait a beat for the new page to finish rendering
    // ------------------------------------------------------------------
    // WHY THE DELAY: this script runs as soon as it is parsed, but on a React
    // or Next.js site the target element may not exist yet — the framework is
    // still hydrating. Scrolling immediately would find nothing and silently
    // fail, right at the moment the user is expecting the payoff.
    //
    // 600ms is a pragmatic guess, the same species of trade-off as WAIT_MS in
    // crawler_js.py. The robust version would poll, or use a MutationObserver
    // to react the instant the element appears:
    //   https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver
    setTimeout(() => highlight(pending), 600);
  }

  // ======================================================================
  //  INGEST — the widget's half of index self-healing
  // ======================================================================
  //  Every page a visitor opens is a freshly rendered snapshot of the site.
  //  We POST that HTML to /ingest, where the server hashes it, skips it if
  //  unchanged, and otherwise re-chunks + re-embeds just this page and merges
  //  it into the site's index. Result: as real people browse, the index
  //  quietly repairs itself — new team members (a mentor added after our last
  //  crawl), new fees, new sections all appear without anyone re-crawling.
  //
  //  Fire-and-forget by design: this must NEVER delay or break the host page,
  //  so no await, no .then chains that matter, everything inside try/catch.
  //  The server's SHA-256 check makes repeat views of an unchanged page cost
  //  ~nothing (one hash compare, zero embedding work).
  //
  //  The delay lets SPA frameworks finish hydrating, so we capture rendered
  //  content rather than an empty <div id="root">. Same trade-off as WAIT_MS
  //  in crawler_js.py: dumb but universal.
  setTimeout(() => {
    try {
      // ONLY INGEST WHEN THE PAGE REALLY BELONGS TO THIS SITE.
      // SITE defaults to location.hostname, so on a genuine deployment they
      // always match. But in local testing (data-site="openlake.in" served
      // from localhost) they differ — and posting dev pages into a real
      // customer's index is poisoning, not self-healing. Skip on mismatch.
      const norm = (s) => String(s || "").toLowerCase().trim()
        .split("//").pop().split("/")[0].split(":")[0].replace(/^www\./, "");
      if (norm(location.hostname) !== norm(SITE)) return;

      const html = document.documentElement.outerHTML;
      // Client-side mirror of the server's size cap — saves everyone the
      // bandwidth when a pathological page somehow exceeds it.
      if (!html || html.length > 3_000_000) return;
      fetch(API + "/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          site_id: SITE,
          url: location.href.split("#")[0],
          title: document.title || "",
          html: html,
        }),
        // keepalive lets the request complete even if the visitor navigates
        // away mid-send — exactly the case on fast cross-page browsing.
        keepalive: true,
      }).catch(() => {});   // network errors are none of the visitor's business
    } catch (e) { /* never let indexing break the host page */ }
  }, 2500);
})();
