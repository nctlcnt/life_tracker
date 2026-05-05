import { useState, useEffect } from 'react';
import { ItemList, ListItem } from './components/ItemList';
import { WeekView, getMonday } from './components/WeekView';
import { MultiLaneTimeline, TimelineEvent } from './components/MultiLaneTimeline';
import { ProjectOverview } from './components/ProjectOverview';
import { RhythmView } from './components/RhythmView';

// ── 日期格式化 ─────────────────────────────────────────────────
const DAY_NAMES_FULL = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

function fmtDateStr(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// ── Tab 类型 ────────────────────────────────────────────────────
type ViewMode = 'day' | 'week' | 'project' | 'rhythm';

export default function App() {
  const [viewMode, setViewMode] = useState<ViewMode>('day');

  // ── Day View State ───────────────────────────────────────────
  const [currentDate, setCurrentDate] = useState(() => fmtDateStr(new Date()));

  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([]);
  const [plannedEvents, setPlannedEvents] = useState<TimelineEvent[]>([]);
  const [cancelledEvents, setCancelledEvents] = useState<TimelineEvent[]>([]);
  const [memories, setMemories] = useState<ListItem[]>([]);
  const [reminders, setReminders] = useState<ListItem[]>([]);
  const [todos, setTodos] = useState<ListItem[]>([]);
  const [deadlines, setDeadlines] = useState<ListItem[]>([]);

  // ── Week View State ──────────────────────────────────────────
  const [weekStart, setWeekStart] = useState(() => getMonday(new Date()));

  // ── 今日显示 ─────────────────────────────────────────────────
  const today = new Date();
  const todayDate = today.getDate();
  const todayMonth = today.getMonth() + 1;
  const todayDayName = DAY_NAMES_FULL[today.getDay()];

  useEffect(() => {
    if (viewMode !== 'day') return;

    const startIso = `${currentDate}T00:00:00`;
    const endIso = `${currentDate}T23:59:59`;
    const dayStart = new Date(startIso);
    const dayEnd = new Date(endIso);

    // Fetch timeline (segments + planned + cancelled)
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

        // Planned / cancelled 是未合并的原始 event，可能无 end_time（时间点型）
        // id 保留真实数字形式，供删除按钮回传后端
        const toPseudoEvent = (e: any): TimelineEvent => {
          const rawStart = new Date(e.start_time);
          // 时间点型（无 end_time）：给一个短时长占位，视觉上还能看到块
          const rawEnd = e.end_time ? new Date(e.end_time) : new Date(rawStart.getTime() + 30 * 60 * 1000);
          return {
            id: String(e.id),
            content: e.content,
            category: e.category,
            startDate: rawStart < dayStart ? dayStart : rawStart,
            endDate: rawEnd > dayEnd ? dayEnd : rawEnd,
            notes: e.notes ?? null,
            project_name: e.project_name ?? null,
          };
        };
        setPlannedEvents((data.planned_events || []).map(toPseudoEvent));
        setCancelledEvents((data.cancelled_events || []).map(toPseudoEvent));
      })
      .catch(err => console.error('Failed to load timeline:', err));

    // Fetch Memories
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

    // Fetch Reminders
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

    // Fetch Todos
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

    // Fetch Deadlines
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

  }, [currentDate, viewMode]);

  const handleDeletePlannedEvent = (id: string) => {
    // 前置乐观更新：planned / cancelled 都可能命中，两个 state 都尝试过滤
    const prevPlanned = plannedEvents;
    const prevCancelled = cancelledEvents;
    setPlannedEvents(list => list.filter(e => e.id !== id));
    setCancelledEvents(list => list.filter(e => e.id !== id));

    fetch(`/api/events/${id}`, { method: 'DELETE' })
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      })
      .catch(err => {
        console.error('Failed to delete event:', err);
        // 回滚
        setPlannedEvents(prevPlanned);
        setCancelledEvents(prevCancelled);
      });
  };

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

  return (
    <div className="size-full bg-background overflow-hidden flex flex-col text-foreground">
      {/* ── Header ────────────────────────────────────────────── */}
      <header className="px-4 py-3 md:px-8 md:py-5 border-b border-border flex flex-col gap-3 md:flex-row md:items-center md:justify-between flex-shrink-0">
        <div className="flex items-baseline gap-2">
          <h1
            className="text-2xl font-bold tracking-tight text-foreground leading-none"
            style={{ fontFamily: "'Inter', 'SF Pro Display', -apple-system, sans-serif" }}
          >
            {todayMonth}月{todayDate}日
          </h1>
          <span className="text-sm text-muted-foreground font-medium">{todayDayName}</span>
        </div>

        <div className="flex flex-wrap items-center gap-2 md:gap-3">
          {/* Tab 切换：日 | 周 | Project Overview | 记忆 */}
          <div className="flex rounded-md border border-border overflow-hidden">
            {(['day', 'week', 'project', 'rhythm'] as ViewMode[]).map((mode) => (
              <button
                key={mode}
                onClick={() => setViewMode(mode)}
                className={`px-3 py-1 text-sm transition-colors ${
                  viewMode === mode
                    ? 'bg-foreground text-background font-medium'
                    : 'bg-transparent text-muted-foreground hover:bg-muted'
                }`}
              >
                {mode === 'day'
                  ? '日'
                  : mode === 'week'
                  ? '周'
                  : mode === 'project'
                  ? 'Project Overview'
                  : 'Rhythm'}
              </button>
            ))}
          </div>

          {/* 日视图的日期导航 */}
          {viewMode === 'day' && (
            <div className="flex items-center gap-2">
              <button
                onClick={() => shiftDate(-1)}
                className="px-2 py-1 rounded border border-border hover:bg-muted text-sm transition-colors"
              >
                ‹
              </button>
              <input
                type="date"
                value={currentDate}
                onChange={e => setCurrentDate(e.target.value)}
                className="px-2 py-1 rounded border border-border bg-transparent text-sm min-w-[125px] outline-none"
              />
              <button
                onClick={() => shiftDate(1)}
                className="px-2 py-1 rounded border border-border hover:bg-muted text-sm transition-colors"
              >
                ›
              </button>
              <button
                onClick={() => setCurrentDate(fmtDateStr(new Date()))}
                className="px-3 py-1 rounded border border-border hover:bg-muted text-sm transition-colors ml-1"
              >
                今天
              </button>
            </div>
          )}
        </div>
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
        <div className="flex-1 min-h-0 overflow-hidden">
          <RhythmView />
        </div>
      ) : (
        /* ── 日视图：桌面=左 1/4 泳道图 + 右 3/4，手机=上下堆叠 ─── */
        <div className="flex-1 flex flex-col md:flex-row min-h-0 overflow-hidden">
          {/* 时间轴：手机顶部 40vh，桌面左侧 1/4 */}
          <div className="w-full h-[40vh] md:w-1/4 md:h-auto flex-shrink-0 border-b border-border md:border-b-0 md:border-r flex flex-col min-h-0">
            <MultiLaneTimeline
              events={timelineEvents}
              plannedEvents={plannedEvents}
              cancelledEvents={cancelledEvents}
              date={currentDate}
              onDeletePlanned={handleDeletePlannedEvent}
            />
          </div>

          {/* 列表区：桌面 2×2，手机 1 列 */}
          <div className="flex-1 min-w-0 overflow-auto px-3 py-3 md:px-6 md:py-6">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 md:gap-4 md:h-full">
              <ItemList title="提醒" items={reminders} type="reminder" />
              <ItemList title="待办" items={todos} type="todo" onToggle={handleTodoToggle} />
              <ItemList title="Deadline" items={deadlines} type="deadline" />
              <ItemList title="记忆" items={memories} type="memory" />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
