// 12 · Can't read page — Chrome blocks extensions on its own pages (chrome://, the Web Store).

import { Kbd } from '../components';
import { openPage, type PageInfo, type RecentItem } from '../data';
import { AlertCircle, External, Lock, MarkDisabled } from '../icons';
import { RecentList } from './Home';

export function CantReadScreen({ page, recent }: { page: PageInfo; recent: RecentItem[] }) {
  return (
    <>
      <div className="page-card">
        <div className="page-card-row no-action">
          <span className="favicon">
            <Lock size={15} />
          </span>
          <div style={{ minWidth: 0, flexGrow: 1 }}>
            <div className="page-url">{page.url || 'Chrome page'}</div>
            <div className="page-state alert">
              <AlertCircle size={13} />
              Can’t read this page
            </div>
          </div>
        </div>
      </div>

      <main className="main" style={{ padding: '0 12px', gap: 0 }}>
        <div className="error-body">
          <div className="error-mark">
            <MarkDisabled />
            <span className="lock">
              <Lock size={13} stroke={2} />
            </span>
          </div>
          <h2>Compass can’t read this page</h2>
          <p>Chrome doesn’t let extensions read its own pages, like Settings or the Web Store. Switch to a website and Compass will analyze it automatically.</p>
          <a href="https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns" target="_blank" rel="noreferrer">
            Which pages are supported
            <External size={13} />
          </a>
        </div>

        {recent.length > 0 && (
          <div style={{ paddingBottom: 12 }}>
            <RecentList
              title="Pick up where you left off"
              recent={recent.slice(0, 3)}
              onAsk={(q) => {
                const item = recent.find((r) => r.question === q);
                if (item) openPage(`https://${item.domain}`);
              }}
            />
          </div>
        )}
      </main>

      <footer className="footer">
        <span>Reads only the active tab</span>
        <span>
          <Kbd>Alt</Kbd>
          <Kbd>C</Kbd>
          <span style={{ marginLeft: 2 }}>to open</span>
        </span>
      </footer>
    </>
  );
}
