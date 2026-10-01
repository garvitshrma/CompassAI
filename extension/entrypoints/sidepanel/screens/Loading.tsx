// 10 · Analyzing page — shown while Compass reads the tab. You can already type.

import { AskBox, PageCard } from '../components';
import type { PageInfo } from '../data';
import { Check, Spinner } from '../icons';

const STEPS = ['Read headings', 'Indexing links and downloads', 'Building the section map'];

export function LoadingScreen({
  page,
  step,
  draft,
  setDraft,
}: {
  page: PageInfo;
  step: number; // how many steps are finished (0–3)
  draft: string;
  setDraft: (v: string) => void;
}) {
  return (
    <>
      <PageCard page={page} analyzing />

      <main className="main">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <AskBox value={draft} onChange={setDraft} onSubmit={() => {}} canSubmit={false} />
          <p className="hint">Start typing — Compass answers as soon as the page is mapped.</p>
        </div>

        <div className="steps">
          <div className="list-head" style={{ padding: 0 }}>
            Mapping this page
          </div>
          {STEPS.map((label, i) => {
            const state = i < step ? 'done' : i === step ? 'now' : 'later';
            return (
              <div key={label} className={`step ${state}`}>
                {state === 'done' ? (
                  <span className="step-done">
                    <Check size={11} stroke={3} />
                  </span>
                ) : state === 'now' ? (
                  <Spinner size={18} width={2.5} />
                ) : (
                  <span className="step-todo" />
                )}
                <span className="step-label">{label}</span>
              </div>
            );
          })}
        </div>

        <div className="list" style={{ gap: 10 }}>
          <div className="list-head" style={{ padding: '0 4px' }}>
            Suggestions for this page
          </div>
          {[62, 48, 56].map((w) => (
            <div key={w} className="sk-row">
              <span className="sk" style={{ width: 28, height: 28, borderRadius: 8 }} />
              <span className="sk" style={{ width: `${w}%`, height: 12 }} />
            </div>
          ))}
        </div>
      </main>

      <footer className="footer">
        <span>Reads only the active tab</span>
        <span>Usually under 2 seconds</span>
      </footer>
    </>
  );
}
