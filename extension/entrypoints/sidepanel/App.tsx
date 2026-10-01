// The side panel's brain: which screen is showing, and what moves it to the next one.
//
//   analyzing ──► home ──(ask)──► result ──(go)──► navigating ──► arrived
//                   │                ▲                  │ Esc
//                   │                └──────────────────┘
//                   └──(ask, nothing found)──► nomatch
//   restricted page (chrome://…) ──► can't read
//
// Page data comes from ./data.ts. Reading the page and scrolling to sections are real;
// answers are still local keyword matching until the backend is connected.

import { useEffect, useRef, useState } from 'react';
import { Header, type Status } from './components';
import {
  addRecent,
  analyzePage,
  askCompass,
  goBack,
  navigateTo,
  recentStore,
  type PageInfo,
  type QueryResult,
  type RecentItem,
  type Section,
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
  | { name: 'navigating'; question: string; result: Found; target: Section; progress: number }
  | { name: 'arrived'; question: string; result: Found; target: Section }
  | { name: 'nomatch'; question: string; result: None };

export default function App() {
  const [page, setPage] = useState<PageInfo | null>(null); // null while analyzing
  const [loadingPage, setLoadingPage] = useState<PageInfo | null>(null); // just the URL, while analyzing
  const [step, setStep] = useState(0);
  const [view, setView] = useState<View>({ name: 'home' });
  const [draft, setDraft] = useState('');
  const [asking, setAsking] = useState(false);
  const [recent, setRecent] = useState<RecentItem[]>([]);
  const navAbort = useRef<AbortController | null>(null);

  // ---- analyze the active tab; runId goes up whenever we need to read it again ----
  const [runId, setRunId] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setStep(0);
    setView({ name: 'home' });
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
    const reread = () => setRunId((n) => n + 1);
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
    const result = await askCompass(page, q);
    setAsking(false);
    setDraft('');
    addRecent({ question: q, domain: page.domain, at: Date.now() });
    setView(result.kind === 'found' ? { name: 'result', question: q, result } : { name: 'nomatch', question: q, result });
  }

  /** Jump straight to a section, e.g. from a heading match or "Other matches". */
  function go(target: Section, question?: string, result?: Found) {
    const q = question ?? ('question' in view ? view.question : target.title);
    const r: Found = result ??
      ('result' in view && view.result.kind === 'found'
        ? view.result
        : { kind: 'found', headline: `I found ${target.noun}.`, best: { section: target, confidence: 'high' }, others: [] });
    setView({ name: 'navigating', question: q, result: r, target, progress: 0 });
  }

  function pickSection(section: Section) {
    const q = draft.trim() || section.title;
    setDraft('');
    go(section, q, { kind: 'found', headline: `I found ${section.noun}.`, best: { section, confidence: 'high' }, others: [] });
  }

  function stop() {
    navAbort.current?.abort();
    if (page) goBack(page);
    if (view.name === 'navigating') setView({ name: 'result', question: view.question, result: view.result });
  }

  // ---- run the scroll while on the navigating screen ----
  const navTarget = view.name === 'navigating' ? view.target : null;
  useEffect(() => {
    if (!navTarget || !page) return;
    const ctrl = new AbortController();
    navAbort.current = ctrl;
    const onProgress = (progress: number) => setView((v) => (v.name === 'navigating' ? { ...v, progress } : v));
    navigateTo(page, navTarget, onProgress, ctrl.signal).then((outcome) => {
      setView((v) => {
        if (v.name !== 'navigating') return v; // the person already moved on
        if (outcome === 'arrived') return { name: 'arrived', question: v.question, result: v.result, target: v.target };
        if (outcome === 'lost')
          return { name: 'result', question: v.question, result: v.result, notice: 'Couldn’t find that section on the page any more. Try analyzing the page again.' };
        return v;
      });
    });
    return () => ctrl.abort();
  }, [navTarget]);

  // ---- panel-wide keys: Esc steps back, Enter on a result means "Go to section" ----
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typingInField = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement;
      const onButton = e.target instanceof HTMLButtonElement;
      if (e.key === 'Escape') {
        if (view.name === 'navigating') stop();
        else if (view.name !== 'home' && !typingInField) setView({ name: 'home' });
      } else if (e.key === 'Enter' && view.name === 'result' && !typingInField && !onButton) {
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
    screen = <ResultScreen page={page} question={view.question} result={view.result} onGo={(s) => go(s)} onAsk={ask} notice={view.notice} />;
  } else if (view.name === 'navigating') {
    screen = <NavigatingScreen page={page} section={view.target} progress={view.progress} onStop={stop} />;
  } else if (view.name === 'arrived') {
    const related = [view.result.best, ...view.result.others].map((m) => m.section).filter((s) => s.id !== view.target.id);
    screen = (
      <ArrivedScreen
        page={page}
        section={view.target}
        related={related.slice(0, 3)}
        onBack={() => {
          goBack(page);
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
