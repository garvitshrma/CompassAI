// 05 · Home (empty box) and 06 · Query (while typing) are the same screen:
// once the box has text, the suggestions and recents swap for live heading matches,
// from this page and from the rest of the website.

import { useState } from 'react';
import { AskBox, Highlight, Kbd, PageCard } from '../components';
import { label, matchHeadings, pageName, pathOf, suggestionsFor, timeAgo, type PageInfo, type RecentItem, type Section, type SiteInfo } from '../data';
import { ArrowRight, ChevronRight, Clock, Hash, Mark, suggestionIcons } from '../icons';

export function HomeScreen({
  page,
  site,
  draft,
  setDraft,
  busy,
  recent,
  onAsk,
  onPickSection,
  onClearRecent,
  onReanalyze,
}: {
  page: PageInfo;
  site: SiteInfo | null;
  draft: string;
  setDraft: (v: string) => void;
  busy: boolean;
  recent: RecentItem[];
  onAsk: (q: string) => void;
  onPickSection: (section: Section) => void;
  onClearRecent: () => void;
  onReanalyze: () => void;
}) {
  const typing = draft.trim().length > 0;
  const { word, here, elsewhere } = matchHeadings(page, draft, site);
  const sections = [...here, ...elsewhere]; // in the order they're shown
  const [selected, setSelected] = useState(0);
  const count = 1 + sections.length; // "Ask Compass" + each heading
  const wholeSite = !!site && (site.state === 'ready' || site.state === 'indexing') && site.sections.length > 0;
  const suggestions = suggestionsFor(page, site);
  const sel = Math.min(selected, count - 1);

  const activate = (i: number) => {
    const section = sections[i - 1];
    if (section) onPickSection(section);
    else onAsk(draft);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (typing && !busy) activate(sel);
    } else if (e.key === 'ArrowDown' && typing) {
      e.preventDefault();
      setSelected((sel + 1) % count);
    } else if (e.key === 'ArrowUp' && typing) {
      e.preventDefault();
      setSelected((sel - 1 + count) % count);
    } else if (e.key === 'Escape') {
      setDraft('');
    }
  };

  return (
    <>
      <PageCard page={page} analyzing={false} onReanalyze={onReanalyze} site={site} />

      <main className="main" style={typing ? { gap: 16 } : undefined}>
        <AskBox
          value={draft}
          onChange={(v) => {
            setDraft(v);
            setSelected(0);
          }}
          onSubmit={() => onAsk(draft)}
          onKeyDown={onKeyDown}
          canSubmit={typing}
          busy={busy}
          active={typing}
          scope={wholeSite ? 'This site' : 'This page'}
        />

        {typing ? (
          <>
            <div className="list">
              <button className={`row primary ${sel === 0 ? 'selected' : ''}`} onClick={() => activate(0)} onMouseEnter={() => setSelected(0)}>
                <span className="row-icon dark">
                  <Mark size={16} color="#FFFDF8" needleOpacity={0.45} />
                </span>
                <span className="row-text">
                  <span className="row-title strong">Ask Compass</span>
                  <span className="row-sub">{wholeSite ? `Explain it from all of ${page.domain}` : 'Find the best match on this page'}</span>
                </span>
                <Kbd>↵</Kbd>
              </button>
            </div>

            {here.length > 0 && (
              <div className="list">
                <div className="list-head">
                  <span>On this page</span>
                  <span className="count">{here.length}</span>
                </div>
                {here.map((section, i) => (
                  <button
                    key={section.id}
                    className={`row ${sel === i + 1 ? 'selected' : ''}`}
                    onClick={() => activate(i + 1)}
                    onMouseEnter={() => setSelected(i + 1)}
                  >
                    <span className="row-icon">
                      <Hash size={15} />
                    </span>
                    <span className="row-text">
                      <span>
                        <Highlight text={section.title} word={word} />
                      </span>
                      <span className="row-meta">{label(section.number, section.group)}</span>
                    </span>
                  </button>
                ))}
              </div>
            )}

            {elsewhere.length > 0 && (
              <div className="list">
                <div className="list-head">
                  <span>Elsewhere on {page.domain}</span>
                  <span className="count">{elsewhere.length}</span>
                </div>
                {elsewhere.map((section, j) => {
                  const i = here.length + j + 1;
                  return (
                    <button
                      key={section.url + section.title}
                      className={`row ${sel === i ? 'selected' : ''}`}
                      onClick={() => activate(i)}
                      onMouseEnter={() => setSelected(i)}
                    >
                      <span className="row-icon">
                        <ArrowRight size={15} />
                      </span>
                      <span className="row-text">
                        <span>
                          <Highlight text={section.title} word={word} />
                        </span>
                        <span className="row-meta">
                          {section.level === 'page' ? label(section.menu && `${section.menu} menu`, pathOf(section.url)) : pageName(section)}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </>
        ) : (
          <>
            {suggestions.length > 0 && (
              <div className="list loose">
                <div className="list-head">Try asking</div>
                {suggestions.map((sug) => {
                  const Icon = suggestionIcons[sug.icon];
                  return (
                    <button key={sug.text} className="row" style={{ minHeight: 40 }} onClick={() => onAsk(sug.text)}>
                      <span className="row-icon">
                        <Icon size={15} />
                      </span>
                      <span className="row-text">{sug.text}</span>
                      <ChevronRight size={15} color="#6B6559" />
                    </button>
                  );
                })}
              </div>
            )}

            {recent.length > 0 && <RecentList recent={recent} onAsk={onAsk} onClear={onClearRecent} />}
          </>
        )}
      </main>

      {typing ? (
        <footer className="footer keys">
          <span>
            <Kbd>↑</Kbd>
            <Kbd>↓</Kbd>select
          </span>
          <span>
            <Kbd>↵</Kbd>go
          </span>
          <span>
            <Kbd>Esc</Kbd>clear
          </span>
        </footer>
      ) : (
        <footer className="footer">
          <span>{wholeSite ? `Searches all of ${page.domain}` : 'Reads only the active tab'}</span>
          <span>
            <Kbd>Alt</Kbd>
            <Kbd>C</Kbd>
            <span style={{ marginLeft: 2 }}>to open</span>
          </span>
        </footer>
      )}
    </>
  );
}

export function RecentList({
  recent,
  onAsk,
  onClear,
  title = 'Recent',
}: {
  recent: RecentItem[];
  onAsk: (q: string) => void;
  onClear?: () => void;
  title?: string;
}) {
  return (
    <div className="list">
      <div className="list-head">
        <span>{title}</span>
        {onClear && (
          <button className="link-btn" onClick={onClear}>
            Clear
          </button>
        )}
      </div>
      {recent.map((r) => (
        <button key={r.at} className="row" style={{ minHeight: 40 }} onClick={() => onAsk(r.question)}>
          <Clock size={15} color="#6B6559" />
          <span className="row-text">
            <span className="row-title clip">{r.question}</span>
            <span className="row-meta">{r.domain}</span>
          </span>
          <span className="row-end">{timeAgo(r.at)}</span>
        </button>
      ))}
    </div>
  );
}
