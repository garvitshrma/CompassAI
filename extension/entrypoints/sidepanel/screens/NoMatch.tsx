// 11 · No match — say so plainly, then offer the closest things Compass did find.

import { Composer, PageBar, RichText } from '../components';
import { label, onThisPage, openPage, pageName, type PageInfo, type QueryResult, type Section } from '../data';
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
            <h2>
              Compass couldn’t find that {result.offline ? 'on this page' : 'on this website'}
            </h2>
            {result.answer ? <RichText text={result.answer} /> : <p>{result.detail}</p>}
            {result.answer && <p className="hint" style={{ padding: 0, marginTop: 6 }}>{result.detail}</p>}
            {result.offline && (
              <p className="hint" style={{ padding: 0, marginTop: 6 }}>
                Couldn’t reach the Compass server, so only keywords were checked. Try again in a minute.
              </p>
            )}
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
                <div className="sub-head">{onThisPage(page, closest.section) ? 'Closest on this page' : `Closest on ${pageName(closest.section)}`}</div>
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
