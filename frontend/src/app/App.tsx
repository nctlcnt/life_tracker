import { useState, useEffect } from 'react';
import { GanttChart } from './components/GanttChart';
import { TimeDistribution } from './components/TimeDistribution';
import { ItemList, ListItem } from './components/ItemList';

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

export default function App() {
  const [currentDate, setCurrentDate] = useState(() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  });

  const [ganttTasks, setGanttTasks] = useState<any[]>([]);
  const [memories, setMemories] = useState<ListItem[]>([]);
  const [reminders, setReminders] = useState<ListItem[]>([]);
  const [todos, setTodos] = useState<ListItem[]>([]);

  useEffect(() => {
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
        setReminders(data.map((r: any) => ({
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
  }, [currentDate]);

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
    setCurrentDate(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`);
  };

  const chartStart = new Date(`${currentDate}T00:00:00`);
  const chartEnd = new Date(`${currentDate}T23:59:59`);

  return (
    <div className="size-full bg-background overflow-auto text-foreground">
      <div className="min-h-full flex flex-col">
        <header className="px-6 py-4 border-b border-border flex items-center justify-between">
          <h1 className="text-lg font-semibold">Life Tracker</h1>
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
              onClick={() => {
                const d = new Date();
                setCurrentDate(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`);
              }}
              className="px-3 py-1 rounded border border-border hover:bg-muted text-sm transition-colors ml-1"
            >
              今天
            </button>
          </div>
        </header>

        <div className="flex-none pt-4 border-b border-border">
            <GanttChart tasks={ganttTasks} startDate={chartStart} endDate={chartEnd} />
        </div>

        <div className="flex-none border-b border-border">
            <TimeDistribution tasks={ganttTasks} catColors={CAT_COLORS} />
        </div>

        <div className="flex-1 px-6 py-6 border-b border-border">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
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
    </div>
  );
}