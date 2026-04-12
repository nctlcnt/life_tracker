import { useState, useEffect, useMemo } from 'react';
import { GanttChart } from './components/GanttChart';
import { TimeDistribution } from './components/TimeDistribution';
import { ItemList, ListItem } from './components/ItemList';
import { WeekView, getMonday } from './components/WeekView';

// 莫兰迪色系：低饱和、柔和的大地色调
const CAT_COLORS: Record<string, string> = {
  '休息': '#C4BFB0', // 暖米灰
  '工作': '#94A3B5', // 烟蓝灰
  '社交': '#A8B9A0', // 鼠尾草绿
  '生活': '#D4AFA0', // 灰粉
  '健康': '#9FBAB0', // 青灰
  '娱乐': '#B5A6BC', // 灰紫
  '出行': '#CFB897', // 沙黄
  'uncategorized': '#B5B1A8', // 灰褐
};

function catColor(cat: string) {
  return CAT_COLORS[cat] || CAT_COLORS['uncategorized'];
}

// ── 日期格式化 ─────────────────────────────────────────────────
const DAY_NAMES_FULL = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

function fmtDateStr(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export default function App() {
  const [viewMode, setViewMode] = useState<'day' | 'week'>('day');

  // ── Day View State ───────────────────────────────────────────
  const [currentDate, setCurrentDate] = useState(() => fmtDateStr(new Date()));

  const [ganttTasks, setGanttTasks] = useState<any[]>([]);
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

    // Fetch timeline
    const startIso = `${currentDate}T00:00:00`;
    const endIso = `${currentDate}T23:59:59`;
    
    fetch(`/api/timeline?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`)
      .then(res => res.json())
      .then(data => {
        const events = data.segments || [];
        const dayStart = new Date(startIso);
        const dayEnd = new Date(endIso);

        let rowMap: Record<string, number> = {};
        let nextRow = 0;

        const tasks = events.map((e: any, idx: number) => {
          if (rowMap[e.category] === undefined) {
             rowMap[e.category] = nextRow++;
          }
          // Clip to day boundaries for cross-day events
          const rawStart = new Date(e.start_time);
          const rawEnd = e.end_time ? new Date(e.end_time) : new Date();
          return {
            id: String(e.id || `s-${idx}`),
            name: e.content || e.category,
            category: e.category,
            startDate: rawStart < dayStart ? dayStart : rawStart,
            endDate: rawEnd > dayEnd ? dayEnd : rawEnd,
            color: catColor(e.category),
            notes: e.notes || null,
            row: rowMap[e.category]
          };
        });

        setGanttTasks(tasks);
      })
      .catch(err => console.error("Failed to load timeline:", err));

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
          // Within same group, sort by trigger_time ascending (nearest first)
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
    // Optionally call backend if exists, for now just toggle state
    setTodos((prev) =>
      prev.map((todo) =>
        todo.id === id ? { ...todo, completed: !todo.completed } : todo
      )
    );
  };

  const shiftDate = (days: number) => {
    const d = new Date(currentDate + 'T00:00:00');
    d.setDate(d.getDate() + days);
    setCurrentDate(fmtDateStr(d));
  };

  const chartStart = new Date(`${currentDate}T00:00:00`);
  const chartEnd = new Date(`${currentDate}T23:59:59`);

  return (
    <div className="size-full bg-background overflow-hidden flex flex-col text-foreground">
        <header className="px-6 py-4 border-b border-border flex items-center justify-between">
          {/* 左上角：今日日期 */}
          <div className="flex items-baseline gap-2">
            <h1 className="text-2xl font-bold tracking-tight text-foreground leading-none" style={{ fontFamily: "'Inter', 'SF Pro Display', -apple-system, sans-serif" }}>
              {todayMonth}月{todayDate}日
            </h1>
            <span className="text-sm text-muted-foreground font-medium">{todayDayName}</span>
          </div>

          {/* 右侧：视图切换 + 日期导航 */}
          <div className="flex items-center gap-3">
            {/* 日/周 切换 */}
            <div className="flex rounded-md border border-border overflow-hidden">
              <button
                onClick={() => setViewMode('day')}
                className={`px-3 py-1 text-sm transition-colors ${
                  viewMode === 'day'
                    ? 'bg-foreground text-background font-medium'
                    : 'bg-transparent text-muted-foreground hover:bg-muted'
                }`}
              >
                日
              </button>
              <button
                onClick={() => setViewMode('week')}
                className={`px-3 py-1 text-sm transition-colors ${
                  viewMode === 'week'
                    ? 'bg-foreground text-background font-medium'
                    : 'bg-transparent text-muted-foreground hover:bg-muted'
                }`}
              >
                周
              </button>
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

        {viewMode === 'week' ? (
          /* ── 周视图 ──────────────────────────────────────── */
          <div className="flex-1 flex flex-col min-h-0">
            <WeekView weekStart={weekStart} onWeekChange={setWeekStart} />
          </div>
        ) : (
          /* ── 日视图（原有内容）──────────────────────────── */
          <div className="flex-1 overflow-auto flex flex-col min-h-0">
            <div className="flex-none pt-4 border-b border-border">
                <GanttChart tasks={ganttTasks} startDate={chartStart} endDate={chartEnd} />
            </div>

            <div className="flex-none border-b border-border">
                <TimeDistribution tasks={ganttTasks} catColors={CAT_COLORS} />
            </div>

            <div className="flex-1 px-6 py-6 border-b border-border">
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-6">
                <ItemList
                  title="Deadline"
                  items={deadlines}
                  type="deadline"
                />
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
              </div>
            </div>
          </div>
        )}
    </div>
  );
}