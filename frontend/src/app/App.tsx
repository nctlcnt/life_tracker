import { useState, useEffect } from 'react';
import { ListItem } from './components/ItemList';
import { WeekView, getMonday } from './components/WeekView';
import { MultiLaneTimeline, TimelineEvent } from './components/MultiLaneTimeline';
import { ProjectOverview } from './components/ProjectOverview';
import { Dashboard } from './components/Dashboard';
import { AdminPanel } from './components/AdminPanel';
import { TraceViewer } from './components/TraceViewer';
import './components/dashboard.css';

// ── 日期格式化 ─────────────────────────────────────────────────
const DAY_NAMES_FULL = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

function fmtDateStr(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// ── Tab 类型 ────────────────────────────────────────────────────
type ViewMode = 'day' | 'week' | 'project' | 'rhythm' | 'memory';

type Route = 'dashboard' | 'admin' | 'traces';

function routeFromHash(): Route {
  const h = window.location.hash.replace(/^#/, '');
  if (h === 'admin') return 'admin';
  if (h === 'traces') return 'traces';
  return 'dashboard';
}

// Top-level router: '#admin' surfaces the hidden admin panel, '#traces'
// surfaces the AI trace viewer; everything else renders the regular
// dashboard. Both auxiliary surfaces are kept off the main nav and only
// reachable by typing the URL hash.
export default function App() {
  const [route, setRoute] = useState<Route>(routeFromHash);
  useEffect(() => {
    const onHash = () => setRoute(routeFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  if (route === 'admin') return <AdminPanel />;
  if (route === 'traces') return <TraceViewer />;
  return <DashboardApp />;
}

function DashboardApp() {
  const [viewMode, setViewMode] = useState<ViewMode>('day');

  // ── Day View State ───────────────────────────────────────────
  const [currentDate, setCurrentDate] = useState(() => fmtDateStr(new Date()));

  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([]);
  const [memories, setMemories] = useState<ListItem[]>([]);
  const [reminders, setReminders] = useState<ListItem[]>([]);
  const [todos, setTodos] = useState<ListItem[]>([]);
  const [deadlines, setDeadlines] = useState<ListItem[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);
  const [appVersion, setAppVersion] = useState<string>('');

  useEffect(() => {
    fetch('/api/version')
      .then(res => res.json())
      .then(data => setAppVersion(data.version || ''))
      .catch(err => console.error('Failed to load version:', err));
  }, []);

  // ── Week View State ──────────────────────────────────────────
  const [weekStart, setWeekStart] = useState(() => getMonday(new Date()));

  // ── 当前选中日期显示 ─────────────────────────────────────────
  const selectedDateObj = new Date(currentDate + 'T00:00:00');
  const headerDate = selectedDateObj.getDate();
  const headerMonth = selectedDateObj.getMonth() + 1;
  const headerDayName = DAY_NAMES_FULL[selectedDateObj.getDay()];
  const headerDayNameEn = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][selectedDateObj.getDay()];

  useEffect(() => {
    // Day + Rhythm both need timeline; Day additionally needs todos /
    // reminders / deadlines for the right rail. Memory only needs memories.
    // Week / Project skip this entirely.
    const needsTimeline = viewMode === 'day' || viewMode === 'rhythm';
    const needsDayLists = viewMode === 'day';
    const needsMemories = viewMode === 'memory';
    if (!needsTimeline && !needsDayLists && !needsMemories) return;

    const startIso = `${currentDate}T00:00:00`;
    const endIso = `${currentDate}T23:59:59`;
    const dayStart = new Date(startIso);
    const dayEnd = new Date(endIso);

    if (needsTimeline) {
      fetch(`/api/timeline?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`)
        .then(res => res.json())
        .then(data => {
          const segments = data.segments || [];
          const events: TimelineEvent[] = segments.map((s: any, idx: number) => {
            const rawStart = new Date(s.start_time);
            const rawEnd = s.end_time ? new Date(s.end_time) : new Date();
            return {
              id: String(s.event_ids?.[0] ?? `s-${idx}`),
              content: s.content,
              category: s.category,
              startDate: rawStart < dayStart ? dayStart : rawStart,
              endDate: rawEnd > dayEnd ? dayEnd : rawEnd,
              notes: s.notes ?? null,
              project_name: s.project_name ?? null,
            };
          });
          setTimelineEvents(events);
        })
        .catch(err => console.error('Failed to load timeline:', err));
    }

    if (needsMemories) {
      fetch('/api/memories')
        .then(res => res.json())
        .then(data => {
          setMemories(data.map((m: any) => ({
            id: String(m.id),
            title: m.content,
            description: `来源: ${m.source === 'user' ? '用户' : 'AI'}`,
          })));
        })
        .catch(err => console.error('Failed to load memories:', err));
    }

    if (needsDayLists) {
      fetch('/api/reminders')
        .then(res => res.json())
        .then(data => {
          const sorted = [...data].sort((a: any, b: any) => {
            const aIsPending = a.status === 'pending' ? 0 : 1;
            const bIsPending = b.status === 'pending' ? 0 : 1;
            if (aIsPending !== bIsPending) return aIsPending - bIsPending;
            return new Date(a.trigger_time).getTime() - new Date(b.trigger_time).getTime();
          });
          setReminders(sorted.map((r: any) => ({
            id: String(r.id),
            title: r.action,
            description: `状态: ${r.status}`,
            dueDate: new Date(r.trigger_time),
            priority: r.priority || 'medium',
          })));
        })
        .catch(err => console.error('Failed to load reminders:', err));

      fetch('/api/todos?all=true')
        .then(res => res.json())
        .then(data => {
          setTodos((data.todos || []).map((t: any) => ({
            id: String(t.id),
            title: t.content,
            completed: t.done === 1 || t.done === true,
          })));
        })
        .catch(err => console.error('Failed to load todos:', err));

      fetch('/api/deadlines')
        .then(res => res.json())
        .then(data => {
          setDeadlines((data.deadlines || []).map((d: any) => ({
            id: String(d.id),
            title: d.title,
            dueDate: new Date(d.due_time),
            countdown: d.countdown,
          })));
        })
        .catch(err => console.error('Failed to load deadlines:', err));
    }

  }, [currentDate, viewMode, refreshKey]);

  const handleTodoToggle = (id: string) => {
    const prev = todos.find((t) => t.id === id);
    if (!prev) return;
    const newDone = !prev.completed;
    setTodos((list) =>
      list.map((todo) =>
        todo.id === id ? { ...todo, completed: newDone } : todo
      )
    );
    fetch(`/api/todos/${id}/done`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ done: newDone }),
    }).then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    }).catch((err) => {
      console.error('Failed to update todo:', err);
      setTodos((list) =>
        list.map((todo) =>
          todo.id === id ? { ...todo, completed: prev.completed } : todo
        )
      );
    });
  };

  const shiftDate = (days: number) => {
    const d = new Date(currentDate + 'T00:00:00');
    d.setDate(d.getDate() + days);
    setCurrentDate(fmtDateStr(d));
  };

  const tabLabels: Record<ViewMode, string> = {
    day: 'Day',
    week: 'Week',
    project: 'Projects',
    rhythm: 'Rhythm',
    memory: 'Memory',
  };
  const tabOrder: ViewMode[] = ['day', 'week', 'project', 'rhythm', 'memory'];
  const showDateNav = viewMode === 'day' || viewMode === 'rhythm';

  return (
    <div className="dash size-full bg-background overflow-hidden flex flex-col text-foreground">
      {/* ── Top bar (editorial) ───────────────────────────────── */}
      <header className="dash-topbar">
        <div className="dash-brand">
          <div className="date">{headerMonth}月{headerDate}日</div>
          <div className="day">{headerDayName} · {headerDayNameEn}</div>
        </div>

        <div className="dash-tabs" role="tablist">
          {tabOrder.map(mode => (
            <button
              key={mode}
              onClick={() => setViewMode(mode)}
              className={viewMode === mode ? 'active' : ''}
            >
              {tabLabels[mode]}
            </button>
          ))}
        </div>

        {showDateNav && (
          <div className="dash-nav">
            <button className="arrow" onClick={() => shiftDate(-1)} title="prev">‹</button>
            <input
              type="date"
              className="date-input"
              value={currentDate}
              onChange={e => setCurrentDate(e.target.value)}
            />
            <button className="arrow" onClick={() => shiftDate(1)} title="next">›</button>
            <button
              className="today"
              onClick={() => setCurrentDate(fmtDateStr(new Date()))}
            >
              Today
            </button>
          </div>
        )}
      </header>

      {/* ── Content ───────────────────────────────────────────── */}
      {viewMode === 'week' ? (
        <div className="flex-1 flex flex-col min-h-0">
          <WeekView weekStart={weekStart} onWeekChange={setWeekStart} />
        </div>
      ) : viewMode === 'project' ? (
        <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
          <ProjectOverview />
        </div>
      ) : viewMode === 'rhythm' ? (
        <div className="flex-1 min-h-0 overflow-auto">
          <div className="rhythm-page">
            <div className="rhythm-card">
              <div className="rhythm-title">Today&apos;s Rhythm</div>
              <div className="multi-lane-frame">
                <MultiLaneTimeline events={timelineEvents} date={currentDate} />
              </div>
            </div>
          </div>
        </div>
      ) : viewMode === 'memory' ? (
        <div className="flex-1 min-h-0 overflow-auto">
          <MemoryPage memories={memories} />
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-auto">
          <Dashboard
            date={currentDate}
            timelineEvents={timelineEvents}
            todos={todos}
            reminders={reminders}
            deadlines={deadlines}
            onTodoToggle={handleTodoToggle}
            onRefresh={() => setRefreshKey(k => k + 1)}
          />
        </div>
      )}

      <footer className="dash-footer">
        {appVersion ? `Life Tracker · ${appVersion}` : 'Life Tracker'}
      </footer>
    </div>
  );
}

// ── Memory tab: editorial reading column of saved notes/quotes ──
function MemoryPage({ memories }: { memories: ListItem[] }) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? memories : memories.slice(0, 12);
  return (
    <div className="memory-page">
      <div className="hero">
        <p className="eyebrow">Memory</p>
        <h1>Saved notes.</h1>
        <p className="welcome">
          {memories.length === 0
            ? 'Nothing saved yet.'
            : `${memories.length} item${memories.length === 1 ? '' : 's'} you wanted to remember.`}
        </p>
      </div>
      {memories.length === 0 ? (
        <div className="memory-empty">Quick notes will show up here.</div>
      ) : (
        <div className="mem-col">
          {visible.map(m => (
            <div key={m.id} className="mem-item">
              <div className="quote">{m.title}</div>
              {m.description && <div className="src">{m.description}</div>}
            </div>
          ))}
          {memories.length > 12 && (
            <button
              type="button"
              className="mem-toggle"
              onClick={() => setShowAll(s => !s)}
            >
              {showAll ? 'Show less ↑' : `Show all ${memories.length} ↓`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
