// 05 · Home (empty box) and 06 · Query (while typing) are the same screen:
// once the box has text, the suggestions and recents swap for live heading matches.

import { useState } from 'react';
import { AskBox, Highlight, Kbd, PageCard } from '../components';
import { label, matchHeadings, timeAgo, type PageInfo, type RecentItem, type Section } from '../data';
import { ChevronRight, Clock, Hash, Mark, suggestionIcons } from '../icons';

export function HomeScreen({
  page,
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
  const { word, sections } = matchHeadings(page, draft);
  const [selected, setSelected] = useState(0);
  const count = 1 + sections.length; // "Ask Compass" + each heading
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
      <PageCard page={page} analyzing={false} onReanalyze={onReanalyze} />

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
                  <span className="row-sub">Find the best match on this page</span>
                </span>
                <Kbd>↵</Kbd>
              </button>
            </div>

            {sections.length > 0 && (
              <div className="list">
                <div className="list-head">
                  <span>Headings that match “{word}”</span>
                  <span className="count">{sections.length}</span>
                </div>
                {sections.map((section, i) => (
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
                      <span className="row-meta">
                        {label(section.number, section.group)}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            {page.suggestions.length > 0 && (
              <div className="list loose">
                <div className="list-head">Try asking</div>
                {page.suggestions.map((sug) => {
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
          <span>Reads only the active tab</span>
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
