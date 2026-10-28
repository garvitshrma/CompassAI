// 08 · Navigating — a mini map of the page while Compass scrolls to the section.
// When the section is on another page, the steps show Compass opening that page
// (through its dropdown menu, if it's in one) and then scrolling there.

import { Composer, Kbd, PageBar } from '../components';
import { label, pageName, type NavStage, type PageInfo, type Section } from '../data';
import { Check, Spinner } from '../icons';

// Fixed bar widths so the mini map looks like a page of text.
const WIDTHS = [70, 50, 62, 44, 58, 40, 66, 48, 56, 38, 60, 52];

export function NavigatingScreen({
  page,
  section,
  progress,
  onStop,
  crossPage = false,
  stage,
  via,
}: {
  page: PageInfo;
  section: Section;
  progress: number; // 0–1 while scrolling; 1 = scrolled, now highlighting
  onStop: () => void;
  crossPage?: boolean;
  stage?: NavStage;
  via?: string; // the dropdown menu Compass opened, e.g. "Programs"
}) {
  if (crossPage) return <CrossPage page={page} section={section} progress={progress} onStop={onStop} stage={stage ?? 'opening'} via={via} />;

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

        <p className="hint">Compass only scrolls and highlights here. It never types or submits anything on the page.</p>
      </main>

      <Composer label="Ask a follow-up" placeholder="Available when Compass arrives…" disabled onSubmit={() => {}} />
    </>
  );
}

const ORDER: NavStage[] = ['opening', 'loading', 'scrolling'];

function Step({ state, title, sub }: { state: 'done' | 'now' | 'later'; title: string; sub?: string }) {
  return (
    <li className={state === 'later' ? 'later' : ''}>
      {state === 'done' ? (
        <span className="step-done">
          <Check size={12} stroke={3} />
        </span>
      ) : state === 'now' ? (
        <Spinner size={20} width={2.5} />
      ) : (
        <span className="step-todo" />
      )}
      <div style={{ minWidth: 0 }}>
        <div className="title">{title}</div>
        {sub && (
          <div style={{ fontSize: 12 }} className="muted">
            {sub}
          </div>
        )}
      </div>
    </li>
  );
}

/** Going to a section on another page of the site. */
function CrossPage({
  page,
  section,
  progress,
  onStop,
  stage,
  via,
}: {
  page: PageInfo;
  section: Section;
  progress: number;
  onStop: () => void;
  stage: NavStage;
  via?: string;
}) {
  const at = progress >= 1 ? ORDER.length : ORDER.indexOf(stage);
  const state = (i: number) => (i < at ? 'done' : i === at ? 'now' : 'later');
  const name = pageName(section);
  const pageOnly = section.level === 'page';

  return (
    <>
      <PageBar page={page} />

      <main className="main mid" style={{ paddingTop: 22 }}>
        <div className="nav-head">
          <span className="live">
            <i className="pulse" />
            {stage === 'opening' ? 'Opening the page…' : stage === 'loading' ? 'Loading…' : 'Finding the section…'}
          </span>
          <h2>Taking you to {pageOnly ? name : section.title}</h2>
        </div>

        <section className="card">
          <div className="progress">
            <div style={{ width: `${progress * 100}%` }} />
          </div>
          <ol className="nav-steps">
            <Step state="done" title="Located it" sub={pageOnly ? name : label(name, section.title)} />
            <Step state={state(0)} title={via ? `Opening the “${via}” menu` : `Opening ${name}`} sub={via ? `Then clicking “${name}”` : undefined} />
            <Step state={state(1)} title="Waiting for the page to load" />
            {!pageOnly && <Step state={state(2)} title="Scrolling to the section and highlighting it" />}
          </ol>
        </section>

        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn big" onClick={onStop}>
            Stop<Kbd>Esc</Kbd>
          </button>
        </div>

        <p className="hint">
          Compass opens menus and follows links like you would. It never types, submits forms or clicks anything else.
        </p>
      </main>

      <Composer label="Ask a follow-up" placeholder="Available when Compass arrives…" disabled onSubmit={() => {}} />
    </>
  );
}
