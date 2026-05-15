import { useState, useEffect, useMemo, Fragment } from 'react';
import { TimelineEvent } from './MultiLaneTimeline';
import { ListItem } from './ItemList';
import './dashboard.css';

interface DashboardProps {
  date: string;
  timelineEvents: TimelineEvent[];
  todos: ListItem[];
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
  metric?: 'check_ins';
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

const fmtClock = (d: Date) =>
  `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;

const fmtDuration = (ms: number) => {
  const totalMin = Math.max(0, Math.round(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
};

const fmtGap = (ms: number) => `${fmtDuration(ms)} later`;

const catClass = (cat: string) =>
  cat === 'Focus' ? 'focus' : cat === 'Chill' ? 'chill' : 'rtn';

const catLabel = (cat: string) =>
  cat === 'Focus' ? 'Focus' : cat === 'Chill' ? 'Chill' : 'Routine';

const fmtMonthDay = (d: Date) =>
  `${d.toLocaleString('en-US', { month: 'long' })} ${d.getDate()}`;

const fmtWeekday = (d: Date) => d.toLocaleString('en-US', { weekday: 'long' });

const fmtShortWeekday = (d: Date) =>
  d.toLocaleString('en-US', { weekday: 'short' });

const fmtShortMonthDay = (d: Date) =>
  `${d.toLocaleString('en-US', { month: 'short' })} ${d.getDate()}`;

const fmtCheckIns = (count: number) => `${count} check-in${count === 1 ? '' : 's'}`;

const fmtUntilDays = (due: Date) => {
  const ms = due.getTime() - Date.now();
  if (ms < 0) return 'overdue';
  const min = Math.round(ms / 60000);
  if (min < 60) return `in ${min}m`;
  const h = Math.round(min / 60);
  if (h < 24) return `in ${h}h`;
  const d = Math.round(h / 24);
  return `in ${d} day${d === 1 ? '' : 's'}`;
};

const GAP_THRESHOLD_MS = 30 * 60 * 1000;
const NOW_WINDOW_MS = 5 * 60 * 1000;

export function Dashboard({
  date,
  timelineEvents,
  todos,
  deadlines,
  reminders,
  onTodoToggle,
  onRefresh,
}: DashboardProps) {
  const [heatmap, setHeatmap] = useState<HeatmapResponse | null>(null);
  const [weather, setWeather] = useState<WeatherResponse | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);

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

  // ── Deadlines: split into next + rest
  const sortedDeadlines = useMemo(
    () =>
      [...deadlines].sort(
        (a, b) => (a.dueDate?.getTime() ?? 0) - (b.dueDate?.getTime() ?? 0)
      ),
    [deadlines]
  );
  const nextDeadline = sortedDeadlines[0];
  const restDeadlines = sortedDeadlines.slice(1);

  // ── Upcoming list = remaining deadlines + pending reminders, merged by date
  const upcoming = useMemo(() => {
    type UpItem = {
      key: string;
      title: string;
      date: Date;
      kind: 'deadline' | 'reminder';
      countdown?: string;
    };
    const fromDeadlines: UpItem[] = restDeadlines
      .filter(d => d.dueDate)
      .map(d => ({
        key: `d-${d.id}`,
        title: d.title,
        date: d.dueDate!,
        kind: 'deadline',
        countdown: d.countdown,
      }));
    const fromReminders: UpItem[] = reminders
      .filter(r => (r.description ?? '').includes('pending') && r.dueDate)
      .map(r => ({
        key: `r-${r.id}`,
        title: r.title,
        date: r.dueDate!,
        kind: 'reminder',
      }));
    return [...fromDeadlines, ...fromReminders].sort(
      (a, b) => a.date.getTime() - b.date.getTime()
    );
  }, [restDeadlines, reminders]);

  const [showAllUpcoming, setShowAllUpcoming] = useState(false);
  const visibleUpcoming = showAllUpcoming ? upcoming : upcoming.slice(0, 3);
  const hasMoreUpcoming = upcoming.length > 3;

  // ── Date pieces for the rail-date card
  const dateObj = new Date(`${date}T00:00:00`);

  // ── Project compact: top 3 by check-ins（已归档不入榜）
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

  // ── Mark the last event as "now" if it's still active / just ended.
  const nowIdx = useMemo(() => {
    if (todayItems.length === 0) return -1;
    const last = todayItems[todayItems.length - 1];
    return last.endDate.getTime() >= Date.now() - NOW_WINDOW_MS
      ? todayItems.length - 1
      : -1;
  }, [todayItems]);

  return (
    <div className="dash-grid">
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

        {/* TODAY — event log */}
        <section className="dash-card">
          <h3 className="h-section">
            Today <span className="count">{todayItems.length} logs</span>
          </h3>
          {todayItems.length === 0 ? (
            <div className="today-empty">No events yet today.</div>
          ) : (
            <div className="today-log">
              {todayItems.map((ev, idx) => {
                const prev = idx > 0 ? todayItems[idx - 1] : null;
                const gapMs = prev ? ev.startDate.getTime() - prev.endDate.getTime() : 0;
                const showGap = gapMs > GAP_THRESHOLD_MS;
                const isNow = idx === nowIdx;
                const cls = isNow ? 'now' : catClass(ev.category);
                return (
                  <Fragment key={ev.id}>
                    {showGap && (
                      <div className="gap-row">
                        <div className="gap-rail" />
                        <div className="gap-label">{fmtGap(gapMs)}</div>
                      </div>
                    )}
                    <div className="log-row">
                      <div className="ts">{fmtClock(ev.startDate)}</div>
                      <div className="rail"><span className={`dot ${cls}`} /></div>
                      <div className="content">
                        <div className="title">{ev.content}</div>
                        <div className="meta">
                          <span className={`cat ${catClass(ev.category)}`}>
                            {catLabel(ev.category)}
                          </span>
                          {ev.project_name && <> · {ev.project_name}</>}
                          {isNow && <> · now</>}
                        </div>
                        {ev.notes && <div className="note">{ev.notes}</div>}
                      </div>
                    </div>
                  </Fragment>
                );
              })}
            </div>
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
        {/* 1. Weather */}
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

        {/* 2. Next deadline */}
        {nextDeadline?.dueDate && (
          <div className="deadline next">
            <p className="label">Next deadline</p>
            <p className="when">
              {fmtWeekday(nextDeadline.dueDate)}<br />
              {fmtMonthDay(nextDeadline.dueDate)} · {fmtClock(nextDeadline.dueDate)}
            </p>
            <p
              className={`countdown${
                (nextDeadline.countdown ?? '').includes('⚠️') ? ' warn' : ''
              }`}
            >
              {nextDeadline.countdown ? `${nextDeadline.countdown} · ` : ''}
              {nextDeadline.title}
            </p>
          </div>
        )}

        {/* 3. Todo */}
        <div className="dash-card">
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
        </div>

        {/* 4. Upcoming reminders */}
        {upcoming.length > 0 && (
          <div className="dash-card">
            <h3 className="h-section">
              Upcoming reminders <span className="count">{upcoming.length}</span>
            </h3>
            {visibleUpcoming.map(u => {
              const isWarn = (u.countdown ?? '').includes('⚠️');
              return (
                <div key={u.key} className="reminder-item">
                  <div className="rm-date">
                    {fmtShortWeekday(u.date)} · {fmtShortMonthDay(u.date)}
                  </div>
                  <div className={`rm-count${isWarn ? ' warn' : ''}`}>
                    {fmtUntilDays(u.date)}
                  </div>
                  <div className="rm-title">
                    {u.title}
                    {u.kind === 'reminder' && (
                      <span style={{ color: 'var(--muted-foreground)' }}>
                        {' · '}
                        {fmtClock(u.date)}
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
            {hasMoreUpcoming && (
              <button
                className="more-dates"
                onClick={() => setShowAllUpcoming(s => !s)}
              >
                {showAllUpcoming ? 'Show less ↑' : 'More ↓'}
              </button>
            )}
          </div>
        )}

        {/* 5. Projects */}
        {projectRows.length > 0 && (
          <div className="pj-card">
            <h3 className="h-section" style={{ marginBottom: 6 }}>
              Projects <span className="count">7d</span>
            </h3>
            {projectRows.map(p => (
              <div key={p.name} className="pj-row">
                <span className="name">{p.name}</span>
                <span className="hrs">{fmtCheckIns(p.total)}</span>
                <div className="pj-bar">
                  {p.perDay.map((count, i) => (
                    <div
                      key={i}
                      className="cell"
                      style={{ opacity: count === 0 ? 0.08 : Math.max(0.15, Math.min(1, count / p.max)) }}
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
