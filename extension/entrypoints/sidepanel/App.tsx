// The side panel's brain: which screen is showing, and what moves it to the next one.
//
//   analyzing ──► home ──(ask)──► result ──(go)──► navigating ──► arrived
//                   │                ▲                  │ Esc
//                   │                └──────────────────┘
//                   └──(ask, nothing found)──► nomatch
//   restricted page (chrome://…) ──► can't read
//
// Page data comes from ./data.ts. Besides the page itself, Compass learns the whole
// website in the background (loadSite), so suggestions and answers can come from any of
// its pages, and "Go to section" can open another page and scroll to the section there.

import { useEffect, useRef, useState } from 'react';
import { Header, type Status } from './components';
import { wakeServer } from './config';
import {
  addRecent,
  analyzePage,
  askCompass,
  goBack,
  goBackPage,
  loadSite,
  navigateTo,
  onThisPage,
  recentStore,
  type NavStage,
  type PageInfo,
  type QueryResult,
  type RecentItem,
  type Section,
  type SiteInfo,
  type Turn,
} from './data';
import { ArrivedScreen } from './screens/Arrived';
import { CantReadScreen } from './screens/CantRead';
import { HomeScreen } from './screens/Home';
import { LoadingScreen } from './screens/Loading';
import { NavigatingScreen } from './screens/Navigating';
import { NoMatchScreen } from './screens/NoMatch';
import { ResultScreen } from './screens/Result';

type Found = Extract<QueryResult, { kind: 'found' }>;
type None = Extract<QueryResult, { kind: 'none' }>;

type View =
  | { name: 'home' }
  | { name: 'result'; question: string; result: Found; notice?: string }
  | { name: 'navigating'; question: string; result: Found; target: Section; progress: number; crossPage: boolean; stage?: NavStage; via?: string }
  | { name: 'arrived'; question: string; result: Found; target: Section; crossPage: boolean; notice?: string }
  | { name: 'nomatch'; question: string; result: None };

export default function App() {
  const [page, setPage] = useState<PageInfo | null>(null); // null while analyzing
  const [loadingPage, setLoadingPage] = useState<PageInfo | null>(null); // just the URL, while analyzing
  const [step, setStep] = useState(0);
  const [view, setView] = useState<View>({ name: 'home' });
  const [draft, setDraft] = useState('');
  const [asking, setAsking] = useState(false);
  const [recent, setRecent] = useState<RecentItem[]>([]);
  const [site, setSite] = useState<SiteInfo | null>(null);
  const [history, setHistory] = useState<Turn[]>([]); // this conversation, for follow-ups
  const navAbort = useRef<AbortController | null>(null);
  const traveling = useRef(false); // Compass is opening another page: ignore tab events meanwhile
  const keepView = useRef(false); // re-read the page but stay on the current screen

  // ---- analyze the active tab; runId goes up whenever we need to read it again ----
  const [runId, setRunId] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const quiet = keepView.current;
    keepView.current = false;
    if (!quiet) {
      setPage(null);
      setStep(0);
      setView({ name: 'home' });
    }
    analyzePage((s, shell) => {
      if (cancelled) return;
      setStep(s);
      setLoadingPage(shell);
    }).then((p) => !cancelled && setPage(p));
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // ---- read again when the person switches tabs, or the tab goes to a new page ----
  useEffect(() => {
    const reread = () => !traveling.current && setRunId((n) => n + 1);
    const onUpdated: Parameters<typeof browser.tabs.onUpdated.addListener>[0] = (_tabId, change, tab) => {
      // change.url: a new URL (also fires for single-page apps like Next.js sites)
      // change.status === 'complete': a page finished loading
      if (tab.active && (change.url || change.status === 'complete')) reread();
    };
    browser.tabs.onActivated.addListener(reread);
    browser.tabs.onUpdated.addListener(onUpdated);
    return () => {
      browser.tabs.onActivated.removeListener(reread);
      browser.tabs.onUpdated.removeListener(onUpdated);
    };
  }, []);

  // ---- learn the whole website (once per site; cached while the panel is open) ----
  // The key only changes when a page has been read, so re-reading the same site keeps what we know.
  const siteKeyRef = useRef('');
  if (page) siteKeyRef.current = page.restricted ? '' : `${page.domain}|${page.tabId}`;
  const siteKey = siteKeyRef.current;
  useEffect(() => {
    if (!page || !siteKey) return setSite(null);
    setSite((s) => (s?.domain === page.domain ? s : null));
    return loadSite(page, setSite);
  }, [siteKey]);

  // A new website starts a new conversation.
  const lastDomain = useRef('');
  useEffect(() => {
    if (page?.domain && page.domain !== lastDomain.current) {
      lastDomain.current = page.domain;
      setHistory([]);
    }
  }, [page?.domain]);

  // ---- wake the backend early (Render's free plan sleeps when idle) ----
  useEffect(wakeServer, []);

  // ---- recent questions live in chrome.storage, and stay in sync ----
  useEffect(() => {
    recentStore.getValue().then(setRecent);
    return recentStore.watch((v) => setRecent(v ?? []));
  }, []);

  // ---- actions ----
  async function ask(question: string) {
    const q = question.trim();
    if (!q || !page || asking) return;
    setAsking(true);
    const result = await askCompass(page, q, history, site);
    setAsking(false);
    setDraft('');
    addRecent({ question: q, domain: page.domain, at: Date.now() });
    const said = result.kind === 'found' ? (result.answer ?? result.headline) : (result.answer ?? result.detail);
    setHistory((h) => [...h, { q, a: said }].slice(-4));
    setView(result.kind === 'found' ? { name: 'result', question: q, result } : { name: 'nomatch', question: q, result });
  }

  /** Jump straight to a section, e.g. from a suggestion, a source or "Other matches". */
  function go(target: Section, question?: string, result?: Found) {
    if (!page) return;
    const q = question ?? ('question' in view ? view.question : target.title);
    const r: Found = result ??
      ('result' in view && view.result.kind === 'found'
        ? view.result
        : { kind: 'found', headline: `I found ${target.noun}.`, best: { section: target, confidence: 'high' }, others: [] });
    setView({ name: 'navigating', question: q, result: r, target, progress: 0, crossPage: !onThisPage(page, target) });
  }

  function pickSection(section: Section) {
    const q = draft.trim() || section.title;
    setDraft('');
    go(section, q, { kind: 'found', headline: `I found ${section.noun}.`, best: { section, confidence: 'high' }, others: [] });
  }

  function stop() {
    navAbort.current?.abort();
    traveling.current = false;
    if (page) goBack(page);
    if (view.name === 'navigating') setView({ name: 'result', question: view.question, result: view.result });
  }

  // ---- run the navigation while on the navigating screen ----
  const navTarget = view.name === 'navigating' ? view.target : null;
  useEffect(() => {
    if (!navTarget || !page) return;
    const ctrl = new AbortController();
    navAbort.current = ctrl;
    const crossPage = !onThisPage(page, navTarget);
    traveling.current = crossPage;
    const onProgress = (progress: number) => setView((v) => (v.name === 'navigating' ? { ...v, progress } : v));
    const onStage = (stage: NavStage, via?: string) =>
      setView((v) => (v.name === 'navigating' ? { ...v, stage, via: via ?? v.via } : v));

    navigateTo(page, navTarget, onProgress, ctrl.signal, onStage).then((outcome) => {
      traveling.current = false;
      if (crossPage && outcome !== 'stopped') {
        // We're on a new page now: read it, but stay on this screen.
        keepView.current = true;
        setRunId((n) => n + 1);
      }
      setView((v) => {
        if (v.name !== 'navigating') return v; // the person already moved on
        const base = { question: v.question, result: v.result, target: v.target, crossPage };
        if (outcome === 'arrived') return { name: 'arrived', ...base };
        if (outcome === 'page')
          return { name: 'arrived', ...base, notice: 'Opened the page, but couldn’t find that exact heading on it. It may be further down.' };
        if (outcome === 'lost')
          return {
            name: 'result',
            question: v.question,
            result: v.result,
            notice: crossPage
              ? 'Couldn’t open that page. Try again, or use “Copy link”.'
              : 'Couldn’t find that section on the page any more. Try analyzing the page again.',
          };
        return v;
      });
    });
    return () => {
      ctrl.abort();
      traveling.current = false;
    };
  }, [navTarget]);

  // ---- panel-wide keys: Esc steps back, Enter on a result means "Go to section" ----
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typingInField = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement;
      const onButton = e.target instanceof HTMLButtonElement;
      if (e.key === 'Escape') {
        if (view.name === 'navigating') stop();
        else if (view.name !== 'home' && !typingInField) setView({ name: 'home' });
      } else if (e.key === 'Enter' && view.name === 'result' && view.result.best && !typingInField && !onButton) {
        go(view.result.best.section);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  // ---- what to show ----
  const status: Status = !page
    ? 'analyzing'
    : page.restricted
      ? 'unavailable'
      : view.name === 'navigating'
        ? 'navigating'
        : asking
          ? 'analyzing'
          : 'ready';

  let screen;
  if (!page) {
    screen = loadingPage && <LoadingScreen page={loadingPage} step={step} draft={draft} setDraft={setDraft} />;
  } else if (page.restricted) {
    screen = <CantReadScreen page={page} recent={recent} />;
  } else if (view.name === 'result') {
    screen = <ResultScreen page={page} question={view.question} result={view.result} onGo={(s) => go(s)} onAsk={ask} notice={view.notice} busy={asking} />;
  } else if (view.name === 'navigating') {
    screen = (
      <NavigatingScreen
        page={page}
        section={view.target}
        progress={view.progress}
        crossPage={view.crossPage}
        stage={view.stage}
        via={view.via}
        onStop={stop}
      />
    );
  } else if (view.name === 'arrived') {
    const related = [...(view.result.best ? [view.result.best.section] : []), ...(view.result.sources ?? []), ...view.result.others.map((m) => m.section)].filter(
      (s, i, all) => s.title !== view.target.title && all.findIndex((x) => x.url === s.url && x.title === s.title) === i,
    );
    screen = (
      <ArrivedScreen
        page={page}
        section={view.target}
        related={related.slice(0, 3)}
        notice={view.notice}
        onBack={() => {
          if (view.crossPage) goBackPage(page);
          else goBack(page);
          setView({ name: 'home' });
        }}
        onGo={(s) => go(s)}
        onAsk={ask}
      />
    );
  } else if (view.name === 'nomatch') {
    screen = <NoMatchScreen page={page} question={view.question} result={view.result} onAsk={ask} onGo={(s) => go(s)} />;
  } else {
    screen = (
      <HomeScreen
        page={page}
        site={site}
        draft={draft}
        setDraft={setDraft}
        busy={asking}
        recent={recent}
        onAsk={ask}
        onPickSection={pickSection}
        onClearRecent={() => recentStore.setValue([])}
        onReanalyze={() => setRunId((n) => n + 1)}
      />
    );
  }

  return (
    <div className="panel">
      <Header status={status} onHome={() => setView({ name: 'home' })} />
      {screen}
    </div>
  );
}
