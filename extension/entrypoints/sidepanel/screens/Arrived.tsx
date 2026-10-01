// 09 · You're here — the section is highlighted on the page.

import { Composer, PageBar } from '../components';
import { label, distance, sectionLink, type PageInfo, type Section } from '../data';
import { ArrowRight, Check, Hash, LinkIcon, Undo } from '../icons';
import { useCopy } from './Result';

export function ArrivedScreen({
  page,
  section,
  related,
  onBack,
  onGo,
  onAsk,
}: {
  page: PageInfo;
  section: Section;
  related: Section[];
  onBack: () => void;
  onGo: (section: Section) => void;
  onAsk: (q: string) => void;
}) {
  const { copied, copy } = useCopy();

  return (
    <>
      <PageBar page={page} hash={section.anchor} />

      <main className="main mid" style={{ paddingTop: 22 }}>
        <div className="arrived-head">
          <span className="pop">
            <Check size={20} stroke={2.5} />
          </span>
          <div>
            <h2>You’re here</h2>
            <p>The section is highlighted on the page.</p>
          </div>
        </div>

        <section className="card">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <span className="crumb">
              {label(section.number, section.group)}
            </span>
            <h3>{section.title}</h3>
          </div>
          {section.points.length > 0 ? (
            <>
              <div className="list-head" style={{ padding: 0 }}>
                In this section
              </div>
              <ul className="bullets">
                {section.points.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </>
          ) : (
            section.summary && <p>{section.summary}</p>
          )}
          <div className="pair" style={{ paddingTop: 4 }}>
            <button className="btn tall strong" onClick={onBack}>
              <Undo size={14} />
              Back to where I was
            </button>
            <button className="btn tall" onClick={() => copy(sectionLink(page, section))}>
              {copied ? <Check size={14} /> : <LinkIcon size={14} />}
              {copied ? 'Copied' : 'Copy link'}
            </button>
          </div>
        </section>

        {related.length > 0 && (
          <div className="list">
            <div className="list-head" style={{ paddingTop: 2 }}>
              Related on this page
            </div>
            {related.map((r) => (
              <button key={r.id} className="row" onClick={() => onGo(r)}>
                <Hash size={15} color="#6B6559" />
                <span className="row-text">
                  <span>{r.title}</span>
                  <span className="row-meta">
                    {label(r.number, distance(page, section.position, r.position))}
                  </span>
                </span>
                <ArrowRight size={15} color="#6B6559" />
              </button>
            ))}
          </div>
        )}
      </main>

      <Composer label="Ask about this section" onSubmit={onAsk} />
    </>
  );
}
