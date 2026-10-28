// Small building blocks shared by several screens.

import { useEffect, useRef, type ReactNode } from 'react';
import type { Confidence, PageInfo, SiteInfo } from './data';
import { ArrowUp, Check, ChevronDown, Clock, MarkTile, Mic, Refresh, Sliders, Spinner } from './icons';

export type Status = 'ready' | 'analyzing' | 'navigating' | 'unavailable';

const STATUS_LABEL: Record<Status, string> = {
  ready: 'Ready',
  analyzing: 'Analyzing',
  navigating: 'Navigating',
  unavailable: 'Unavailable',
};

export function Header({ status, onHome }: { status: Status; onHome: () => void }) {
  const tone = status === 'ready' ? '' : status === 'unavailable' ? 'off' : 'busy';
  return (
    <header className="header">
      <MarkTile />
      <span className="brand">Compass</span>
      <span className={`status ${tone}`} role="status">
        <span className={`dot ${tone === 'busy' ? 'pulse' : ''}`} />
        {STATUS_LABEL[status]}
      </span>
      <div className="header-actions">
        <button className="ib" aria-label="History" title="History" onClick={onHome}>
          <Clock />
        </button>
        <button className="ib" aria-label="Settings" title="Settings (coming soon)">
          <Sliders />
        </button>
      </div>
    </header>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

function Url({ page }: { page: PageInfo }) {
  return (
    <>
      {page.domain}
      <span className="path">{page.path}</span>
    </>
  );
}

/** The big page card at the top of Home, Query and Loading. */
/** "· 12 sections · Learning the site…" / "· 12 sections · 9 pages" */
function SiteState({ site }: { site?: SiteInfo | null }) {
  if (!site) return null;
  if (site.state === 'loading' || site.state === 'indexing')
    return (
      <span className="muted" style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
        · <Spinner size={10} width={3} /> Learning the site{site.pages > 1 ? ` (${site.pages} pages)` : '…'}
      </span>
    );
  if (site.state === 'ready' && site.pages > 1) return <span className="muted">· {site.pages} pages</span>;
  return null;
}

export function PageCard({
  page,
  analyzing,
  onReanalyze,
  site,
}: {
  page: PageInfo;
  analyzing: boolean;
  onReanalyze?: () => void;
  site?: SiteInfo | null;
}) {
  return (
    <div className="page-card">
      <div className={`page-card-row ${analyzing ? 'no-action' : ''}`}>
        <span className="favicon">{page.badge}</span>
        <div style={{ minWidth: 0, flexGrow: 1 }}>
          <div className="page-url">
            <Url page={page} />
          </div>
          {analyzing ? (
            <div className="page-state" style={{ gap: 6 }}>
              <Spinner />
              Analyzing page…
            </div>
          ) : (
            <div className="page-state">
              <Check size={13} />
              Page analyzed<span className="muted">· {page.sections.length} sections</span>
              <SiteState site={site} />
            </div>
          )}
        </div>
        {!analyzing && (
          <button className="ib sm" aria-label="Analyze page again" title="Analyze page again" onClick={onReanalyze}>
            <Refresh size={16} />
          </button>
        )}
      </div>
      {analyzing && (
        <div className="indet-track">
          <div className="indet-bar" />
        </div>
      )}
    </div>
  );
}

/** The compact page strip used once there's an answer on screen. */
export function PageBar({ page, hash = '' }: { page: PageInfo; hash?: string }) {
  return (
    <div className="page-bar">
      <span className="favicon">{page.badge}</span>
      <span className="page-url">
        {page.domain}
        <span className="path">
          {page.path}
          {hash}
        </span>
      </span>
      <span className="ok">
        <Check size={12} stroke={2.5} />
        Analyzed
      </span>
    </div>
  );
}

/** The large question box on Home / Query / Loading. */
export function AskBox({
  value,
  onChange,
  onSubmit,
  onKeyDown,
  canSubmit,
  busy,
  active,
  scope = 'This page',
}: {
  scope?: string;
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  canSubmit: boolean;
  busy?: boolean;
  active?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => ref.current?.focus(), []);

  return (
    <div className={`ask ${active ? 'active' : ''}`}>
      <label htmlFor="ask" className="vh">
        Ask Compass
      </label>
      <textarea
        id="ask"
        ref={ref}
        rows={3}
        placeholder="What are you looking for?"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="ask-tools">
        <button className="scope" title={scope === 'This site' ? 'Compass searches every page of this website' : 'Compass searches this page'}>
          {scope}
          <ChevronDown size={13} />
        </button>
        <span className="grow" />
        <button className="ib" aria-label="Ask by voice" title="Voice (coming soon)">
          <Mic />
        </button>
        <button className="send" aria-label="Find" disabled={!canSubmit || busy} onClick={onSubmit}>
          {busy ? <Spinner size={16} width={2.5} /> : <ArrowUp />}
        </button>
      </div>
    </div>
  );
}

/** The one-line follow-up box pinned to the bottom of answer screens. */
export function Composer({
  label,
  placeholder,
  disabled,
  onSubmit,
}: {
  label: string;
  placeholder?: string;
  disabled?: boolean;
  onSubmit: (q: string) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const send = () => {
    const q = ref.current?.value.trim();
    if (q) {
      onSubmit(q);
      ref.current!.value = '';
    }
  };
  return (
    <div className="composer-wrap">
      <form
        className={`composer ${disabled ? 'disabled' : ''}`}
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <label htmlFor="followup" className="vh">
          {label}
        </label>
        <input id="followup" ref={ref} type="text" placeholder={placeholder ?? label + '…'} disabled={disabled} autoComplete="off" />
        <button type="button" className="ib" aria-label="Ask by voice" disabled={disabled} title="Voice (coming soon)">
          <Mic />
        </button>
        <button type="submit" className="send" aria-label="Send" disabled={disabled}>
          <ArrowUp />
        </button>
      </form>
    </div>
  );
}

export function ConfidenceBadge({ level, label }: { level: Confidence; label?: string }) {
  const text = label ?? { high: 'High match', medium: 'Medium', low: 'Low' }[level];
  return (
    <span className={`conf ${level}`}>
      <span className="bars">
        <i />
        <i />
        <i />
      </span>
      {text}
    </span>
  );
}

/** Wraps every occurrence of `word` in <mark>, e.g. "<mark>Refund</mark>s and Cancellations". */
export function Highlight({ text, word }: { text: string; word: string }) {
  if (!word) return <>{text}</>;
  const parts = text.split(new RegExp(`(${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'ig'));
  return (
    <>
      {parts.map((part, i) => (i % 2 === 1 ? <mark key={i}>{part}</mark> : part))}
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Answers written by the server's AI                                  */
/* ------------------------------------------------------------------ */

// The answer comes as simple markdown: paragraphs, "- " or "1. " lists, **bold**, `code`
// and citations like [2]. We turn it into React elements ourselves (never innerHTML),
// so nothing in an answer can run as code in the panel.

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[\d+(?:\s*,\s*\d+)*\])/g;

function Inline({ text, sources, onCite }: { text: string; sources: number; onCite: (n: number) => void }) {
  return (
    <>
      {text.split(INLINE).map((part, i) => {
        if (i % 2 === 0) return part;
        if (part.startsWith('**')) return <strong key={i}>{part.slice(2, -2)}</strong>;
        if (part.startsWith('`')) return <code key={i}>{part.slice(1, -1)}</code>;
        const nums = part.slice(1, -1).split(',').map((n) => Number(n.trim())).filter((n) => n >= 1 && n <= sources);
        return nums.map((n) => (
          <button key={`${i}-${n}`} className="cite" title={`Source ${n}`} onClick={() => onCite(n)}>
            {n}
          </button>
        ));
      })}
    </>
  );
}

export function RichText({ text, sources = 0, onCite = () => {} }: { text: string; sources?: number; onCite?: (n: number) => void }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flush = () => {
    if (!list) return;
    const items = list.items.map((item, i) => (
      <li key={i}>
        <span>
          <Inline text={item} sources={sources} onCite={onCite} />
        </span>
      </li>
    ));
    blocks.push(list.ordered ? <ol key={blocks.length}>{items}</ol> : <ul key={blocks.length} className="bullets">{items}</ul>);
    list = null;
  };

  for (const raw of text.split('\n')) {
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)/);
    const numbered = line.match(/^\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const ordered = !bullet;
      if (list && list.ordered !== ordered) flush();
      list ??= { ordered, items: [] };
      list.items.push((bullet ?? numbered)?.[1] ?? '');
    } else if (!line) {
      flush();
    } else {
      flush();
      const heading = line.match(/^#{1,6}\s+(.*)/);
      blocks.push(
        <p key={blocks.length} className={heading ? 'strong' : undefined}>
          <Inline text={heading?.[1] ?? line} sources={sources} onCite={onCite} />
        </p>,
      );
    }
  }
  flush();
  return <div className="rich">{blocks}</div>;
}
