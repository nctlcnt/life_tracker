import { useState, useEffect } from 'react';
import { ItemList, ListItem } from './components/ItemList';
import { WeekView, getMonday } from './components/WeekView';

// 三分法色系（新）+ 旧分类兜底
const CAT_COLORS: Record<string, string> = {
  'Focus':   '#94A3B5', // 烟蓝灰 — 专注/投入
  'Routine': '#C4BFB0', // 暖米灰 — 日常维护
  'Chill':   '#A8B9A0', // 鼠尾草绿 — 蓄水放松
  // 旧分类兜底（存量数据）
  '休息': '#C4BFB0',
  '工作': '#94A3B5',
  '社交': '#A8B9A0',
  '生活': '#D4AFA0',
  '健康': '#9FBAB0',
  '娱乐': '#B5A6BC',
  '出行': '#CFB897',
  'uncategorized': '#B5B1A8',
};

// ── 日期格式化 ─────────────────────────────────────────────────
const DAY_NAMES_FULL = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

function fmtDateStr(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// ── Tab 类型 ────────────────────────────────────────────────────
type ViewMode = 'day' | 'week' | 'project';

export default function App() {
  const [viewMode, setViewMode] = useState<ViewMode>('day');

  // ── Day View State ───────────────────────────────────────────
  const [currentDate, setCurrentDate] = useState(() => fmtDateStr(new Date()));

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
      .catch(err => console.error("Failed to load memories:", err));

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
      .catch(err => console.error("Failed to load reminders:", err));

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
      .catch(err => console.error("Failed to load todos:", err));

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
      .catch(err => console.error("Failed to load deadlines:", err));
  }, [currentDate, viewMode]);

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
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ done: newDone }),
    }).then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    }).catch((err) => {
      console.error("Failed to update todo:", err);
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
      <header className="px-8 py-5 border-b border-border flex items-center justify-between flex-shrink-0">
        <div className="flex items-baseline gap-2">
          <h1 className="text-2xl font-bold tracking-tight text-foreground leading-none" style={{ fontFamily: "'Inter', 'SF Pro Display', -apple-system, sans-serif" }}>
            {todayMonth}月{todayDate}日
          </h1>
          <span className="text-sm text-muted-foreground font-medium">{todayDayName}</span>
        </div>

        <div className="flex items-center gap-3">
          {/* 三 Tab 切换：日 | 周 | Project Overview */}
          <div className="flex rounded-md border border-border overflow-hidden">
            {(['day', 'week', 'project'] as ViewMode[]).map((mode) => (
              <button
                key={mode}
                onClick={() => setViewMode(mode)}
                className={`px-3 py-1 text-sm transition-colors ${
                  viewMode === mode
                    ? 'bg-foreground text-background font-medium'
                    : 'bg-transparent text-muted-foreground hover:bg-muted'
                }`}
              >
                {mode === 'day' ? '日' : mode === 'week' ? '周' : 'Project Overview'}
              </button>
            ))}
          </div>

          {/* 日视图的日期导航 */}
          {viewMode === 'day' && (
            <div className="flex items-center gap-2">
              <button onClick={() => shiftDate(-1)} className="px-2 py-1 rounded border border-border hover:bg-muted text-sm transition-colors">‹</button>
              <input
                type="date"
                value={currentDate}
                onChange={e => setCurrentDate(e.target.value)}
                className="px-2 py-1 rounded border border-border bg-transparent text-sm min-w-[125px] outline-none"
              />
              <button onClick={() => shiftDate(1)} className="px-2 py-1 rounded border border-border hover:bg-muted text-sm transition-colors">›</button>
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
        <ProjectOverviewPlaceholder />
      ) : (
        /* ── 日视图：左 1/4 + 右 3/4 ─────────────────────── */
        <div className="flex-1 flex min-h-0 overflow-hidden">
          {/* 左 1/4：多泳道时间轴（占位符） */}
          <div className="w-1/4 flex-shrink-0 border-r border-border flex flex-col">
            <TimelinePlaceholder />
          </div>

          {/* 右 3/4 */}
          <div className="flex-1 flex flex-col min-w-0 overflow-auto">
            {/* 右上：蓄水/漏水比例图（占位符） */}
            <div className="flex-shrink-0 border-b border-border">
              <DistributionPlaceholder />
            </div>

            {/* 右下：2×2 四方块 */}
            <div className="flex-1 px-6 py-6">
              <div className="grid grid-cols-2 gap-4 h-full">
                <ItemList
                  title="记忆"
                  items={memories}
                  type="memory"
                />
                <ItemList
                  title="提醒"
                  items={reminders}
                  type="reminder"
                />
                <ItemList
                  title="待办"
                  items={todos}
                  type="todo"
                  onToggle={handleTodoToggle}
                />
                <ItemList
                  title="Deadline"
                  items={deadlines}
                  type="deadline"
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── 占位符：多泳道时间轴 ─────────────────────────────────────────
function TimelinePlaceholder() {
  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-3 p-6 text-center">
      <div className="flex gap-1.5">
        {(['Focus', 'Routine', 'Chill'] as const).map((lane) => (
          <div
            key={lane}
            className="flex flex-col items-center gap-1.5"
          >
            <span
              className="text-[10px] tracking-wide font-medium"
              style={{ color: 'var(--color-muted-foreground)' }}
            >
              {lane}
            </span>
            <div
              className="w-5 rounded-sm opacity-40"
              style={{
                height: 120,
                backgroundColor: CAT_COLORS[lane],
              }}
            />
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground leading-relaxed mt-1">
        多泳道时间轴
        <br />
        <span className="opacity-60">coming soon</span>
      </p>
    </div>
  );
}

// ── 占位符：蓄水/漏水比例图 ──────────────────────────────────────
function DistributionPlaceholder() {
  return (
    <div className="h-24 flex items-center justify-center gap-6 px-8">
      <div className="flex items-center gap-2">
        <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: CAT_COLORS['Chill'] }} />
        <span className="text-xs text-muted-foreground">蓄水</span>
      </div>
      <div
        className="flex-1 h-2 rounded-full opacity-20"
        style={{ backgroundColor: CAT_COLORS['Chill'], maxWidth: 160 }}
      />
      <span className="text-xs text-muted-foreground opacity-50">蓄水/漏水比例图 coming soon</span>
      <div
        className="flex-1 h-2 rounded-full opacity-20"
        style={{ backgroundColor: '#D4AFA0', maxWidth: 160 }}
      />
      <div className="flex items-center gap-2">
        <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: '#D4AFA0' }} />
        <span className="text-xs text-muted-foreground">漏水</span>
      </div>
    </div>
  );
}

// ── 占位符：Project Overview ─────────────────────────────────────
function ProjectOverviewPlaceholder() {
  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-4 text-center p-8">
      <div className="grid grid-cols-7 gap-1 opacity-30">
        {Array.from({ length: 91 }).map((_, i) => (
          <div
            key={i}
            className="w-4 h-4 rounded-sm"
            style={{
              backgroundColor: CAT_COLORS['Focus'],
              opacity: Math.random() > 0.6 ? Math.random() * 0.8 + 0.2 : 0.08,
            }}
          />
        ))}
      </div>
      <p className="text-sm text-muted-foreground">
        Project Overview — GitHub 式热力图
        <br />
        <span className="text-xs opacity-60">coming soon</span>
      </p>
    </div>
  );
}
