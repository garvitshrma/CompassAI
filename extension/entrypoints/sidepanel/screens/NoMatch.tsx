// 11 · No match — say so plainly, then offer the closest things Compass did find.

import { Composer, PageBar } from '../components';
import { label, openPage, type PageInfo, type QueryResult, type Section } from '../data';
import { ArrowRight, Hash, SearchOff, Tag } from '../icons';

type None = Extract<QueryResult, { kind: 'none' }>;

export function NoMatchScreen({
  page,
  question,
  result,
  onAsk,
  onGo,
}: {
  page: PageInfo;
  question: string;
  result: None;
  onAsk: (q: string) => void;
  onGo: (section: Section) => void;
}) {
  const { link, closest } = result;

  return (
    <>
      <PageBar page={page} />

      <main className="main mid" style={{ paddingTop: 18 }}>
        <div className="turn">
          <span className="who">YOU</span>
          <p>{question}</p>
        </div>

        <div className="nomatch-head">
          <span className="tile">
            <SearchOff size={19} />
          </span>
          <div>
            <h2>Nothing on this page mentions “{result.topic}”</h2>
            <p>{result.detail}</p>
          </div>
        </div>

        {(link || closest) && (
          <section className="card flat">
            {link && (
              <>
                <div className="sub-head">Linked from this page</div>
                <button className="row" onClick={() => openPage(link.url)}>
                  <span className="row-icon soft">
                    <Tag size={15} />
                  </span>
                  <span className="row-text">
                    <span className="row-title strong">{link.label}</span>
                    <span className="row-meta">{link.url.replace(/^https?:\/\//, '')}</span>
                  </span>
                  <ArrowRight size={15} color="#17160F" />
                </button>
              </>
            )}
            {link && closest && <div className="divider" />}
            {closest && (
              <>
                <div className="sub-head">Closest on this page</div>
                <button className="row" onClick={() => onGo(closest.section)}>
                  <span style={{ margin: '0 6px', display: 'flex' }}>
                    <Hash size={15} color="#6B6559" />
                  </span>
                  <span className="row-text">
                    <span>{closest.section.title}</span>
                    <span className="row-meta">
                      {label(closest.section.number, closest.section.group)}
                    </span>
                  </span>
                  <span className="row-end">Low</span>
                </button>
              </>
            )}
          </section>
        )}

        {result.alternatives.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '0 4px' }}>
            <div className="list-head" style={{ padding: 0 }}>
              Try different words
            </div>
            <div className="chips">
              {result.alternatives.map((alt) => (
                <button key={alt} className="chip" onClick={() => onAsk(alt)}>
                  {alt}
                </button>
              ))}
            </div>
          </div>
        )}
      </main>

      <Composer label="Ask something else" onSubmit={onAsk} />
    </>
  );
}
