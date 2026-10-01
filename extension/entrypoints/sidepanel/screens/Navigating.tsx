// 08 · Navigating — a mini map of the page while Compass scrolls to the section.

import { Composer, Kbd, PageBar } from '../components';
import { label, type PageInfo, type Section } from '../data';
import { Check, Spinner } from '../icons';

// Fixed bar widths so the mini map looks like a page of text.
const WIDTHS = [70, 50, 62, 44, 58, 40, 66, 48, 56, 38, 60, 52];

export function NavigatingScreen({
  page,
  section,
  progress,
  onStop,
}: {
  page: PageInfo;
  section: Section;
  progress: number; // 0–1 while scrolling; 1 = scrolled, now highlighting
  onStop: () => void;
}) {
  // Show up to 12 sections of the page, in a window that includes the target.
  const all = page.sections.findIndex((s) => s.id === section.id);
  const first = Math.max(0, Math.min(all - 6, page.sections.length - WIDTHS.length));
  const sections = page.sections.slice(first, first + WIDTHS.length);
  const targetIndex = Math.max(0, all - first);
  const targetTop = (targetIndex / sections.length) * 100 - 6;
  const viewTop = 2 + (Math.max(targetTop, 2) - 2) * progress;

  const totalScreens = Math.max(1, Math.round(section.position * page.screens));
  const doneScreens = Math.min(totalScreens, Math.round(progress * totalScreens));
  const scrolled = progress >= 1;

  return (
    <>
      <PageBar page={page} />

      <main className="main mid" style={{ paddingTop: 22 }}>
        <div className="nav-head">
          <span className="live">
            <i className="pulse" />
            {scrolled ? 'Highlighting…' : 'Finding the section…'}
          </span>
          <h2>Navigating to {section.title}</h2>
        </div>

        <section className="card nav-card">
          <div className="map" aria-label="Page map">
            {sections.map((s, i) => (
              <div key={s.id} className={`sec ${i === targetIndex ? 'target' : ''}`}>
                <i style={{ width: `${WIDTHS[i]}%` }} />
                <b />
              </div>
            ))}
            <span className="view" style={{ top: `${viewTop}%` }} />
          </div>

          <ol className="nav-steps">
            <li>
              <span className="step-done">
                <Check size={12} stroke={3} />
              </span>
              <div style={{ minWidth: 0 }}>
                <div className="title">Located the section</div>
                <div style={{ fontSize: 12 }} className="muted">
                  {label(section.number, section.title)}
                </div>
              </div>
            </li>
            <li>
              {scrolled ? (
                <span className="step-done">
                  <Check size={12} stroke={3} />
                </span>
              ) : (
                <Spinner size={20} width={2.5} />
              )}
              <div style={{ minWidth: 0, flexGrow: 1 }}>
                <div className="title">Scrolling to it</div>
                <div className="progress">
                  <div style={{ width: `${progress * 100}%` }} />
                </div>
                <div style={{ fontSize: 12 }} className="muted">
                  {doneScreens} of {totalScreens} screen{totalScreens === 1 ? '' : 's'}
                </div>
              </div>
            </li>
            <li className={scrolled ? '' : 'later'}>
              {scrolled ? <Spinner size={20} width={2.5} /> : <span className="step-todo" />}
              <div style={{ minWidth: 0 }}>
                <div className="title">Highlight on the page</div>
              </div>
            </li>
          </ol>
        </section>

        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn big" onClick={onStop}>
            Stop<Kbd>Esc</Kbd>
          </button>
        </div>

        <p className="hint">Compass only scrolls and highlights. It never clicks, types or submits anything on the page.</p>
      </main>

      <Composer label="Ask a follow-up" placeholder="Available when Compass arrives…" disabled onSubmit={() => {}} />
    </>
  );
}
