// Small building blocks shared by several screens.

import { useEffect, useRef, type ReactNode } from 'react';
import type { Confidence, PageInfo } from './data';
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
export function PageCard({ page, analyzing, onReanalyze }: { page: PageInfo; analyzing: boolean; onReanalyze?: () => void }) {
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
}: {
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
        <button className="scope" title="Search scope (only this page for now)">
          This page
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
