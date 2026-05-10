import { useState, useEffect, useMemo, Fragment } from 'react';
import { MultiLaneTimeline, TimelineEvent } from './MultiLaneTimeline';
import { ListItem } from './ItemList';
import './dashboard.css';

interface DashboardProps {
  date: string;
  timelineEvents: TimelineEvent[];
  todos: ListItem[];
  memories: ListItem[];
  reminders: ListItem[];
  deadlines: ListItem[];
  onTodoToggle: (id: string) => void;
  onRefresh: () => void;
}

interface HeatmapResponse {
  projects: string[];
  dates: string[];
  data: Record<string, Record<string, number>>;
  archived?: string[];
}

interface WeatherResponse {
  available: boolean;
  temp?: number;
  condition?: string;
  max_temp?: number;
  min_temp?: number;
  location?: string;
}

type ModalKind = 'event' | 'todo' | 'note' | null;

const fmtTimeRange = (s: Date, e: Date) => {
  const f = (d: Date) =>
    `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return `${f(s)} — ${f(e)}`;
};

const fmtDuration = (ms: number) => {
  const totalMin = Math.max(0, Math.round(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
};

const laneClass = (cat: string) =>
  cat === 'Focus' ? 'ld-focus' : cat === 'Chill' ? 'ld-chill' : 'ld-rtn';

const fmtMonthDay = (d: Date) =>
  `${d.toLocaleString('en-US', { month: 'long' })} ${d.getDate()}`;

const fmtWeekday = (d: Date) => d.toLocaleString('en-US', { weekday: 'long' });

const fmtMinutes = (m: number) => {
  const h = Math.floor(m / 60);
  const mm = Math.round(m % 60);
  if (h === 0) return `${mm}m`;
  if (mm === 0) return `${h}h`;
  return `${h}h ${mm}m`;
};

const fmtUntil = (due: Date) => {
  const ms = due.getTime() - Date.now();
  if (ms < 0) return 'overdue';
  const min = Math.round(ms / 60000);
  if (min < 1) return 'now';
  if (min < 60) return `in ${min}m`;
  const h = Math.round(min / 60);
  if (h < 24) return `in ${h}h`;
  return `in ${Math.round(h / 24)}d`;
};

const fmtClock = (d: Date) =>
  `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;

export function Dashboard({
  date,
  timelineEvents,
  todos,
  memories,
  deadlines,
  reminders,
  onTodoToggle,
  onRefresh,
}: DashboardProps) {
  const [heatmap, setHeatmap] = useState<HeatmapResponse | null>(null);
  const [weather, setWeather] = useState<WeatherResponse | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [showAllMemories, setShowAllMemories] = useState(false);

  // Snapshot of uncompleted todo IDs at last data refresh. Single toggles
  // (which only flip `completed` on the same IDs) won't update this set, so
  // a just-checked todo remains visible until the next refresh.
  const todoIdsKey = useMemo(
    () => todos.map(t => t.id).join('|'),
    [todos]
  );
  const [visibleTodoIds, setVisibleTodoIds] = useState<Set<string>>(
    () => new Set(todos.filter(t => !t.completed).map(t => t.id))
  );
  useEffect(() => {
    setVisibleTodoIds(new Set(todos.filter(t => !t.completed).map(t => t.id)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [todoIdsKey]);
  const visibleTodos = todos.filter(t => visibleTodoIds.has(t.id));

  useEffect(() => {
    fetch('/api/projects/heatmap?days=7')
      .then(r => r.json())
      .then(d => setHeatmap(d))
      .catch(err => console.error('Failed to load projects heatmap:', err));

    fetch('/api/weather')
      .then(r => r.json())
      .then(d => setWeather(d))
      .catch(err => console.error('Failed to load weather:', err));
  }, []);

  const todayItems = useMemo(
    () =>
      [...timelineEvents].sort(
        (a, b) => a.startDate.getTime() - b.startDate.getTime()
      ),
    [timelineEvents]
  );

  // ── Hero stat: today's actual focus minutes + pending reminder count
  const todayFocusMs = useMemo(
    () =>
      timelineEvents
        .filter(e => e.category === 'Focus')
        .reduce((sum, e) => sum + (e.endDate.getTime() - e.startDate.getTime()), 0),
    [timelineEvents]
  );
  const pendingReminderCount = reminders.filter(
    r => (r.description ?? '').includes('pending')
  ).length;

  // ── Deadlines: split into next + upcoming
  const sortedDeadlines = useMemo(
    () =>
      [...deadlines].sort(
        (a, b) => (a.dueDate?.getTime() ?? 0) - (b.dueDate?.getTime() ?? 0)
      ),
    [deadlines]
  );
  const [showAllDates, setShowAllDates] = useState(false);
  const visibleDeadlines = showAllDates ? sortedDeadlines : sortedDeadlines.slice(0, 3);
  const hasMoreDates = sortedDeadlines.length > 3;

  // ── Next pending reminders (next 1–2) shown subtly under "Next deadline"
  const nextReminders = useMemo(
    () =>
      reminders
        .filter(r => (r.description ?? '').includes('pending') && r.dueDate)
        .slice(0, 2),
    [reminders]
  );

  // ── Date pieces for the rail-date card
  const dateObj = new Date(`${date}T00:00:00`);

  // ── Project compact: top 3 by total（已归档不入榜）
  const projectRows = useMemo(() => {
    if (!heatmap) return [];
    const archived = new Set(heatmap.archived ?? []);
    const totals = heatmap.projects
      .filter(p => !archived.has(p))
      .map(p => {
        const perDay = heatmap.dates.map(d => heatmap.data[p]?.[d] ?? 0);
        const sum = perDay.reduce((a, b) => a + b, 0);
        const max = Math.max(...perDay, 1);
        return { name: p, total: sum, perDay, max };
      });
    return totals
      .filter(r => r.total > 0)
      .sort((a, b) => b.total - a.total)
      .slice(0, 3);
  }, [heatmap]);

  return (
    <div className="dash-grid">
      {/* ── Left rail ──────────────────────────────────────── */}
      <aside className="rail-rhythm">
        <div className="rhythm-title">Today&apos;s Rhythm</div>
        <div className="multi-lane-frame">
          <MultiLaneTimeline
            events={timelineEvents}
            date={date}
          />
        </div>
      </aside>

      {/* ── Center column ──────────────────────────────────── */}
      <main className="col-center">
        <div className="hero">
          <p className="eyebrow">Dashboard</p>
          <h1>Welcome back.</h1>
          <p className="welcome">
            {todayFocusMs > 0 ? (
              <>
                You logged <b>{fmtDuration(todayFocusMs)} of Focus</b> today.
              </>
            ) : (
              <>No Focus blocks logged yet today.</>
            )}{' '}
            {pendingReminderCount > 0 && (
              <>
                <b>{pendingReminderCount}</b> reminder{pendingReminderCount === 1 ? '' : 's'} need a look.
              </>
            )}
          </p>
        </div>

        {/* TODAY */}
        <section className="dash-card">
          <h3 className="h-section">
            Today <span className="count">{todayItems.length} events</span>
          </h3>
          {todayItems.length === 0 ? (
            <div className="today-empty">No events yet today.</div>
          ) : (
            <div className="today-list">
              {todayItems.map(ev => {
                const dur = ev.endDate.getTime() - ev.startDate.getTime();
                return (
                  <div key={ev.id} className="today-item">
                    <div className="time">{fmtTimeRange(ev.startDate, ev.endDate)}</div>
                    <div className={`lane-dot ${laneClass(ev.category)}`} />
                    <div className="body">
                      <div className="title">
                        {ev.project_name ? (
                          <>
                            <span style={{ color: 'var(--muted-foreground)' }}>
                              {ev.project_name} ·{' '}
                            </span>
                            {ev.content}
                          </>
                        ) : (
                          ev.content
                        )}
                      </div>
                      <div className="sub">
                        {ev.category} · {fmtDuration(dur)}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        {/* TODOS */}
        <section className="dash-card">
          <h3 className="h-section">
            To-do{' '}
            <span className="count">
              {todos.filter(t => !t.completed).length} of {todos.length}
            </span>
          </h3>
          {visibleTodos.length === 0 ? (
            <div className="today-empty">
              {todos.length === 0 ? 'No todos.' : 'All caught up.'}
            </div>
          ) : (
            visibleTodos.map(t => (
              <div
                key={t.id}
                className={`dash-todo${t.completed ? ' done' : ''}`}
                onClick={() => onTodoToggle(t.id)}
              >
                <span className="box">
                  {t.completed && (
                    <svg viewBox="0 0 16 16" fill="none">
                      <path
                        d="M3 8.5l3.5 3.5 7-7"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  )}
                </span>
                <span className="label">{t.title}</span>
                {t.priority === 'high' && <span className="pri high" />}
                {t.priority === 'medium' && <span className="pri med" />}
              </div>
            ))
          )}
        </section>

        {/* MEMORIES */}
        <section className="dash-card">
          <h3 className="h-section">
            Memory{' '}
            <span className="count">
              {showAllMemories ? `all ${memories.length}` : 'recent'}
            </span>
          </h3>
          {memories.length === 0 ? (
            <div className="today-empty">No memories yet.</div>
          ) : (
            <>
              {(showAllMemories ? memories : memories.slice(0, 6)).map(m => (
                <div key={m.id} className="mem-item">
                  <div className="quote">{m.title}</div>
                  {m.description && <div className="src">{m.description}</div>}
                </div>
              ))}
              {memories.length > 6 && (
                <button
                  type="button"
                  className="mem-toggle"
                  onClick={() => setShowAllMemories(s => !s)}
                >
                  {showAllMemories
                    ? 'Show less ↑'
                    : `Show all ${memories.length} ↓`}
                </button>
              )}
            </>
          )}
        </section>

        {/* Action bar */}
        <div className="actionbar">
          <button type="button" onClick={() => setModal('event')}>
            <svg viewBox="0 0 16 16" fill="none">
              <path d="M8 14V2M2 8h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            New event
          </button>
          <button type="button" onClick={() => setModal('todo')}>
            <svg viewBox="0 0 16 16" fill="none">
              <rect x="3" y="3" width="10" height="10" rx="1" stroke="currentColor" strokeWidth="1.5" />
              <path d="M8 6v4M6 8h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            New todo
          </button>
          <button type="button" onClick={() => setModal('note')}>
            <svg viewBox="0 0 16 16" fill="none">
              <path d="M3 4h10M3 8h10M3 12h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            Quick note
          </button>
        </div>
      </main>

      {/* ── Right rail ─────────────────────────────────────── */}
      <aside className="rail-right">
        {/* Weather / date */}
        <div className="weather">
          <div className="top">
            <svg viewBox="0 0 32 32" fill="none">
              <circle cx="16" cy="16" r="5.5" stroke="currentColor" strokeWidth="1.5" />
              <g stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M16 4v3" />
                <path d="M16 25v3" />
                <path d="M4 16h3" />
                <path d="M25 16h3" />
                <path d="M7.5 7.5l2 2" />
                <path d="M22.5 22.5l2 2" />
                <path d="M24.5 7.5l-2 2" />
                <path d="M9.5 22.5l-2 2" />
              </g>
            </svg>
            <div className="meta">
              <div className="when">
                {fmtWeekday(dateObj)},<br />
                {fmtMonthDay(dateObj)}
              </div>
              <div className="temp">
                {weather?.available && weather.temp != null
                  ? `${weather.temp}° · ${weather.condition ?? ''}`
                  : '—° · —'}
              </div>
            </div>
          </div>
          {weather?.available && weather.min_temp != null && weather.max_temp != null && (
            <a href="#" onClick={e => e.preventDefault()}>
              {weather.min_temp}° / {weather.max_temp}° · {weather.location ?? ''}
            </a>
          )}
        </div>

        {/* Reminder bell icon + body — extracted so we can render standalone when there are no deadlines */}
        {(() => {
          const reminderCards = nextReminders.map(r => (
            <div key={`r-${r.id}`} className="reminder-item">
              <svg className="bell" viewBox="0 0 16 16" fill="none">
                <path
                  d="M8 2v1M5 5a3 3 0 016 0v3l1.5 2h-9L5 8V5zM6.5 12.5a1.5 1.5 0 003 0"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <div className="body">
                <div className="title">{r.title}</div>
                <div className="countdown">
                  {r.dueDate ? `${fmtUntil(r.dueDate)} · ${fmtClock(r.dueDate)}` : ''}
                </div>
              </div>
            </div>
          ));

          if (visibleDeadlines.length === 0) {
            return reminderCards;
          }

          return visibleDeadlines.map((d, idx) => {
            const isNext = idx === 0;
            const isWarn = (d.countdown ?? '').includes('⚠️');
            const dueLabel = d.dueDate
              ? `${fmtWeekday(d.dueDate)}\n${fmtMonthDay(d.dueDate)}`
              : '';
            const card = (
              <div key={`d-${d.id}`} className={`deadline${isNext ? ' next' : ''}`}>
                <p className="label">{isNext ? 'Next deadline' : 'Upcoming'}</p>
                <p className="when">
                  {dueLabel.split('\n').map((line, i) => (
                    <span key={i}>
                      {line}
                      {i === 0 && <br />}
                    </span>
                  ))}
                </p>
                <p className={`countdown${isWarn ? ' warn' : ''}`}>
                  {d.countdown ? `${d.countdown} · ` : ''}
                  {d.title}
                </p>
              </div>
            );
            if (!isNext || reminderCards.length === 0) return card;
            return (
              <Fragment key={`d-block-${d.id}`}>
                {card}
                {reminderCards}
              </Fragment>
            );
          });
        })()}

        {hasMoreDates && (
          <button className="more-dates" onClick={() => setShowAllDates(s => !s)}>
            {showAllDates ? 'Show less ↑' : 'More dates ↓'}
          </button>
        )}

        {/* Project compact */}
        {projectRows.length > 0 && (
          <div className="pj-card">
            <h3 className="h-section" style={{ marginBottom: 6 }}>
              Projects <span className="count">7d</span>
            </h3>
            {projectRows.map(p => (
              <div key={p.name} className="pj-row">
                <span className="name">{p.name}</span>
                <span className="hrs">{fmtMinutes(p.total)}</span>
                <div className="pj-bar">
                  {p.perDay.map((m, i) => (
                    <div
                      key={i}
                      className="cell"
                      style={{ opacity: m === 0 ? 0.08 : Math.max(0.15, Math.min(1, m / p.max)) }}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </aside>

      {modal && (
        <ActionModal
          kind={modal}
          onClose={() => setModal(null)}
          onCreated={() => {
            setModal(null);
            onRefresh();
          }}
        />
      )}
    </div>
  );
}

// ── Action modal ──────────────────────────────────────────────
interface ActionModalProps {
  kind: 'event' | 'todo' | 'note';
  onClose: () => void;
  onCreated: () => void;
}

function ActionModal({ kind, onClose, onCreated }: ActionModalProps) {
  const [content, setContent] = useState('');
  const [category, setCategory] = useState<'Focus' | 'Routine' | 'Chill'>('Routine');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const titles = {
    event: 'New event',
    todo: 'New todo',
    note: 'Quick note',
  } as const;
  const placeholders = {
    event: 'What are you doing right now?',
    todo: 'What needs to get done?',
    note: 'What do you want to remember?',
  } as const;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      let url = '';
      let body: Record<string, unknown> = { content: content.trim() };
      if (kind === 'event') {
        url = '/api/events';
        body = { ...body, category };
      } else if (kind === 'todo') {
        url = '/api/todos';
      } else {
        url = '/api/memories';
      }
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed');
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={e => e.stopPropagation()} onSubmit={submit}>
        <h3 className="h-section" style={{ marginBottom: 14 }}>
          {titles[kind]}
        </h3>
        <textarea
          className="modal-input"
          placeholder={placeholders[kind]}
          value={content}
          onChange={e => setContent(e.target.value)}
          autoFocus
          rows={kind === 'note' ? 4 : 2}
        />
        {kind === 'event' && (
          <div className="modal-row">
            <label className="modal-label">Category</label>
            <div className="modal-cat-group">
              {(['Focus', 'Routine', 'Chill'] as const).map(c => (
                <button
                  key={c}
                  type="button"
                  className={`modal-cat${category === c ? ' active' : ''}`}
                  onClick={() => setCategory(c)}
                >
                  {c}
                </button>
              ))}
            </div>
          </div>
        )}
        {error && <div className="modal-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="modal-btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="modal-btn primary"
            disabled={!content.trim() || submitting}
          >
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>
      </form>
    </div>
  );
}
