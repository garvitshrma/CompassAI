// 07 · Result — the best match, where it is on the page, and one big "Go to section" button.

import { useState } from 'react';
import { Composer, ConfidenceBadge, PageBar } from '../components';
import { label, distance, sectionLink, type PageInfo, type QueryResult, type Section } from '../data';
import { ArrowRight, Check, Hash, LinkIcon, Lines, Mark } from '../icons';

type Found = Extract<QueryResult, { kind: 'found' }>;

/** Copies text and briefly swaps the label to "Copied". */
export function useCopy() {
  const [copied, setCopied] = useState(false);
  const copy = async (text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return { copied, copy };
}

export function ResultScreen({
  page,
  question,
  result,
  onGo,
  onAsk,
  notice,
}: {
  page: PageInfo;
  question: string;
  result: Found;
  onGo: (section: Section) => void;
  onAsk: (q: string) => void;
  notice?: string;
}) {
  const { section, confidence } = result.best;
  const [summary, setSummary] = useState(false);
  const { copied, copy } = useCopy();
  const viewSize = 1 / page.screens; // how much of the page fits on one screen

  return (
    <>
      <PageBar page={page} />

      <main className="main tight">
        <div className="turn">
          <span className="who">YOU</span>
          <p>{question}</p>
        </div>

        <div className="turn answer">
          <span className="who">
            <Mark />
          </span>
          <p>{result.headline}</p>
        </div>

        <section className="card">
          <div className="card-top">
            <span className="crumb">
              {[section.group, section.number].filter(Boolean).join(' › ')}
            </span>
            <ConfidenceBadge level={confidence} />
          </div>
          <h2>{section.title}</h2>
          {result.explanation ? <p>{result.explanation}</p> : section.summary && <p>{section.summary}</p>}
          {summary && (
            <ul className="bullets">
              {section.points.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}

          <div className="where" aria-label={`Section position: ${distance(page, page.viewTop, section.position)}`}>
            <div className="where-track">
              <span className="where-view" style={{ left: `${page.viewTop * 100}%`, width: `${Math.max(viewSize * 100, 2)}%` }} />
              <span className="where-dot" style={{ left: `${section.position * 100}%` }} />
            </div>
            <div className="where-labels">
              <span>Your view</span>
              <span>{distance(page, page.viewTop, section.position)}</span>
            </div>
          </div>

          {result.offline && (
            <p className="hint" style={{ padding: 0 }}>
              Couldn’t reach the Compass server, so this match is based on keywords only.
            </p>
          )}

          {notice && (
            <p role="alert" style={{ color: 'var(--alert)', fontSize: 12.5 }}>
              {notice}
            </p>
          )}

          <button className="go" onClick={() => onGo(section)}>
            <span>
              <ArrowRight size={17} stroke={2} />
              Go to section
            </span>
            <span className="go-key">↵</span>
          </button>

          <div className="pair">
            <button
              className="btn"
              onClick={() => copy(sectionLink(page, section))}
              style={section.points.length === 0 ? { gridColumn: '1 / -1' } : undefined}
            >
              {copied ? <Check size={14} /> : <LinkIcon size={14} />}
              {copied ? 'Copied' : 'Copy link'}
            </button>
            {section.points.length > 0 && (
              <button className="btn" onClick={() => setSummary(!summary)} aria-pressed={summary}>
                <Lines size={14} />
                {summary ? 'Hide summary' : 'Summarize'}
              </button>
            )}
          </div>
        </section>

        {result.others.length > 0 && (
          <div className="list">
            <div className="list-head" style={{ paddingTop: 2 }}>
              <span>Other matches</span>
              <span className="count">{result.others.length}</span>
            </div>
            {result.others.map((m) => (
              <button key={m.section.id} className="row" onClick={() => onGo(m.section)}>
                <Hash size={15} color="#6B6559" />
                <span className="row-text">
                  <span>{m.section.title}</span>
                  <span className="row-meta">
                    {label(m.section.number, m.section.group)}
                  </span>
                </span>
                <span className="row-end" style={{ color: m.confidence === 'low' ? undefined : '#4A463D' }}>
                  {{ high: 'High', medium: 'Medium', low: 'Low' }[m.confidence]}
                </span>
              </button>
            ))}
          </div>
        )}
      </main>

      <Composer label="Ask a follow-up" onSubmit={onAsk} />
    </>
  );
}
