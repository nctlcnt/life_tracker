import { useState, useEffect, useMemo } from 'react';
import { motion } from 'motion/react';
import { CalendarClock, Clock, StickyNote } from 'lucide-react';

// ── 莫兰迪色系（与 App 保持一致）──────────────────────────────
const CAT_COLORS: Record<string, string> = {
  '休息': '#C4BFB0',
  '工作': '#94A3B5',
  '社交': '#A8B9A0',
  '生活': '#D4AFA0',
  '健康': '#9FBAB0',
  '娱乐': '#B5A6BC',
  '出行': '#CFB897',
  'uncategorized': '#B5B1A8',
};

function catColor(cat: string) {
  return CAT_COLORS[cat] || CAT_COLORS['uncategorized'];
}

// ── 类型 ─────────────────────────────────────────────────────
interface Segment {
  id: number;
  content: string;
  category: string;
  start_time: string;
  end_time: string | null;
  notes: string | null;
}

interface Deadline {
  id: number;
  title: string;
  due_time: string;
  countdown: string;
}

interface DayData {
  date: Date;
  dateStr: string; // YYYY-MM-DD
  segments: Segment[];
}

// ── 工具函数 ──────────────────────────────────────────────────
const DAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

/** 获取 date 所在周的周一（ISO 周一起始） */
function getMonday(date: Date): Date {
  const d = new Date(date);
  const day = d.getDay(); // 0=Sun, 1=Mon, …
  const diff = day === 0 ? -6 : 1 - day;
  d.setDate(d.getDate() + diff);
  d.setHours(0, 0, 0, 0);
  return d;
}

function fmtDateStr(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function fmtTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

function fmtDuration(startIso: string, endIso: string | null) {
  if (!endIso) return '';
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  const totalMin = Math.max(0, Math.round(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h === 0) return `${m}分钟`;
  if (m === 0) return `${h}小时`;
  return `${h}h${m}m`;
}

function isSameDay(a: Date, b: Date) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

// ── 组件 Props ────────────────────────────────────────────────
interface WeekViewProps {
  weekStart: Date; // 周一的日期
  onWeekChange: (monday: Date) => void;
}

export function WeekView({ weekStart, onWeekChange }: WeekViewProps) {
  const [daysData, setDaysData] = useState<DayData[]>([]);
  const [deadlines, setDeadlines] = useState<Deadline[]>([]);
  const [loading, setLoading] = useState(true);

  // 生成周一到周日的 7 个日期
  const weekDays = useMemo(() => {
    const days: Date[] = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(weekStart);
      d.setDate(d.getDate() + i);
      days.push(d);
    }
    return days;
  }, [weekStart]);

  const weekEnd = weekDays[6]; // 周日

  // 获取数据
  useEffect(() => {
    setLoading(true);

    const startIso = `${fmtDateStr(weekDays[0])}T00:00:00`;
    const endIso = `${fmtDateStr(weekDays[6])}T23:59:59`;

    // 并行 fetch timeline + deadlines
    Promise.all([
      fetch(`/api/timeline?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`)
        .then(r => r.json()),
      fetch('/api/deadlines')
        .then(r => r.json()),
    ])
      .then(([timelineData, deadlineData]) => {
        const segments: Segment[] = timelineData.segments || [];

        // 按日分组
        const dayMap = new Map<string, Segment[]>();
        for (const day of weekDays) {
          dayMap.set(fmtDateStr(day), []);
        }

        for (const seg of segments) {
          const segDate = new Date(seg.start_time);
          const key = fmtDateStr(segDate);
          if (dayMap.has(key)) {
            dayMap.get(key)!.push(seg);
          }
        }

        const days: DayData[] = weekDays.map(day => ({
          date: day,
          dateStr: fmtDateStr(day),
          segments: dayMap.get(fmtDateStr(day)) || [],
        }));

        setDaysData(days);

        // 过滤本周范围内的 deadline
        const weekStartTime = weekDays[0].getTime();
        const weekEndDate = new Date(weekDays[6]);
        weekEndDate.setHours(23, 59, 59, 999);
        const weekEndTime = weekEndDate.getTime();

        const weekDeadlines = (deadlineData.deadlines || []).filter((d: Deadline) => {
          const t = new Date(d.due_time).getTime();
          return t >= weekStartTime && t <= weekEndTime;
        });

        setDeadlines(weekDeadlines);
        setLoading(false);
      })
      .catch(err => {
        console.error('Failed to load week data:', err);
        setLoading(false);
      });
  }, [weekStart]);

  // 判断某一天是否有 deadline
  const deadlineDays = useMemo(() => {
    const set = new Set<string>();
    for (const d of deadlines) {
      const dt = new Date(d.due_time);
      set.add(fmtDateStr(dt));
    }
    return set;
  }, [deadlines]);

  const today = new Date();
  const todayStr = fmtDateStr(today);

  // ── 周导航 ────────────────────────────────────────────────
  const shiftWeek = (weeks: number) => {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + weeks * 7);
    onWeekChange(d);
  };

  const goThisWeek = () => {
    onWeekChange(getMonday(new Date()));
  };

  // 周范围标签
  const weekLabel = `${weekDays[0].getMonth() + 1}/${weekDays[0].getDate()} — ${weekEnd.getMonth() + 1}/${weekEnd.getDate()}`;

  return (
    <div className="flex flex-col h-full">
      {/* 周导航栏 */}
      <div className="px-6 py-3 flex items-center gap-3 border-b border-border">
        <button
          onClick={() => shiftWeek(-1)}
          className="px-2 py-1 rounded border border-border hover:bg-muted text-sm transition-colors"
        >
          ‹
        </button>
        <span className="text-sm text-foreground font-medium tabular-nums min-w-[100px] text-center">
          {weekLabel}
        </span>
        <button
          onClick={() => shiftWeek(1)}
          className="px-2 py-1 rounded border border-border hover:bg-muted text-sm transition-colors"
        >
          ›
        </button>
        <button
          onClick={goThisWeek}
          className="px-3 py-1 rounded border border-border hover:bg-muted text-sm transition-colors"
        >
          本周
        </button>
      </div>

      {/* 主网格 */}
      {loading ? (
        <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
          加载中…
        </div>
      ) : (
        <div className="flex-1 overflow-auto px-4 py-4 sm:px-6">
          <div
            className="grid gap-3"
            style={{
              gridTemplateColumns: 'repeat(3, 1fr)',
              gridTemplateRows: 'auto auto auto',
            }}
          >
            {/* 周一 ~ 周六（前 6 天）*/}
            {daysData.slice(0, 6).map((day, idx) => (
              <DayCard
                key={day.dateStr}
                day={day}
                dayName={DAY_NAMES[idx]}
                isToday={day.dateStr === todayStr}
                hasDeadline={deadlineDays.has(day.dateStr)}
                index={idx}
              />
            ))}

            {/* 周日 */}
            {daysData[6] && (
              <DayCard
                key={daysData[6].dateStr}
                day={daysData[6]}
                dayName={DAY_NAMES[6]}
                isToday={daysData[6].dateStr === todayStr}
                hasDeadline={deadlineDays.has(daysData[6].dateStr)}
                index={6}
              />
            )}

            {/* Deadline 面板 — 合并后两格 */}
            <DeadlinePanel deadlines={deadlines} style={{ gridColumn: 'span 2' }} />
          </div>
        </div>
      )}
    </div>
  );
}

// ── 日卡片 ────────────────────────────────────────────────────
interface DayCardProps {
  day: DayData;
  dayName: string;
  isToday: boolean;
  hasDeadline: boolean;
  index: number;
}

function DayCard({ day, dayName, isToday, hasDeadline, index }: DayCardProps) {
  const dateNum = day.date.getDate();
  const notesEvents = day.segments.filter(s => s.notes);

  return (
    <motion.div
      className={`
        rounded-lg border overflow-hidden flex flex-col
        ${isToday
          ? 'border-foreground/20 bg-muted/30 shadow-sm'
          : 'border-border bg-background hover:bg-muted/20'
        }
        transition-colors
      `}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay: index * 0.04 }}
    >
      {/* 日历头 */}
      <div className={`
        px-3 py-2 flex items-baseline gap-2 border-b
        ${isToday ? 'border-foreground/10' : 'border-border'}
      `}>
        <span className={`
          text-xl font-semibold tabular-nums leading-none
          ${isToday ? 'text-foreground' : 'text-foreground/80'}
        `}>
          {dateNum}
        </span>
        <span className={`
          text-xs
          ${isToday ? 'text-foreground/60' : 'text-muted-foreground'}
        `}>
          {dayName}
        </span>
        {isToday && (
          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-foreground/8 text-foreground/50 font-medium leading-none">
            今天
          </span>
        )}
        {hasDeadline && (
          <span className="ml-auto w-2 h-2 rounded-full bg-destructive flex-shrink-0" title="有 Deadline" />
        )}
      </div>

      {/* 事件列表 */}
      <div className="flex-1 px-3 py-2 space-y-1.5 min-h-[60px] max-h-[200px] overflow-y-auto">
        {day.segments.length === 0 ? (
          <div className="text-xs text-muted-foreground/50 italic py-2">无事件</div>
        ) : (
          day.segments.map((seg, i) => (
            <div key={seg.id || i} className="flex items-start gap-2 group">
              {/* 类别圆点 */}
              <span
                className="w-2 h-2 rounded-full mt-[5px] flex-shrink-0"
                style={{ backgroundColor: catColor(seg.category) }}
              />
              <div className="flex-1 min-w-0">
                <div className="text-xs text-foreground leading-snug truncate">
                  {seg.content}
                </div>
                <div className="text-[10px] text-muted-foreground tabular-nums flex items-center gap-1 mt-0.5">
                  <span>{fmtTime(seg.start_time)}</span>
                  {seg.end_time && (
                    <>
                      <span className="opacity-40">—</span>
                      <span>{fmtTime(seg.end_time)}</span>
                      <span className="opacity-40 mx-0.5">·</span>
                      <span>{fmtDuration(seg.start_time, seg.end_time)}</span>
                    </>
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* 笔记区域 */}
      {notesEvents.length > 0 && (
        <div className="border-t border-border/60 px-3 py-2 space-y-1">
          <div className="flex items-center gap-1 text-muted-foreground mb-0.5">
            <StickyNote className="w-3 h-3" />
            <span className="text-[10px] font-medium">笔记</span>
          </div>
          {notesEvents.map((seg, i) => (
            <div key={`note-${seg.id || i}`} className="text-[11px] text-muted-foreground leading-relaxed pl-4">
              <span className="text-foreground/60 mr-1">{seg.content}:</span>
              <span className="whitespace-pre-line">{seg.notes}</span>
            </div>
          ))}
        </div>
      )}
    </motion.div>
  );
}

// ── Deadline 合并面板 ──────────────────────────────────────────
interface DeadlinePanelProps {
  deadlines: Deadline[];
  style?: React.CSSProperties;
}

function DeadlinePanel({ deadlines, style }: DeadlinePanelProps) {
  return (
    <motion.div
      className="rounded-lg border border-border bg-background overflow-hidden flex flex-col"
      style={style}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay: 0.3 }}
    >
      {/* 头部 */}
      <div className="px-4 py-2.5 border-b border-border flex items-center gap-2">
        <CalendarClock className="w-4 h-4 text-muted-foreground" />
        <span className="text-sm font-medium text-foreground">本周 Deadline</span>
        <span className="ml-auto text-xs text-muted-foreground">{deadlines.length}</span>
      </div>

      {/* 列表 */}
      <div className="flex-1 px-4 py-2 space-y-2 overflow-y-auto max-h-[200px]">
        {deadlines.length === 0 ? (
          <div className="text-xs text-muted-foreground/50 italic py-4 text-center">本周暂无 Deadline</div>
        ) : (
          deadlines.map((d, i) => {
            const due = new Date(d.due_time);
            const dayIdx = (due.getDay() + 6) % 7; // 0=Mon, …, 6=Sun
            return (
              <motion.div
                key={d.id}
                className="flex items-start gap-2 py-1"
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.35 + i * 0.05 }}
              >
                <span className="w-2 h-2 rounded-full bg-destructive mt-[5px] flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-foreground leading-snug">{d.title}</div>
                  <div className="text-[10px] text-muted-foreground mt-0.5 flex items-center gap-1.5">
                    <span>{DAY_NAMES[dayIdx]}</span>
                    <span className="opacity-40">·</span>
                    <span>{due.getMonth() + 1}/{due.getDate()} {fmtTime(d.due_time)}</span>
                  </div>
                  {d.countdown && (
                    <div className={`text-[10px] mt-0.5 font-medium ${
                      d.countdown.includes('⚠️') ? 'text-destructive' : 'text-muted-foreground'
                    }`}>
                      {d.countdown}
                    </div>
                  )}
                </div>
              </motion.div>
            );
          })
        )}
      </div>
    </motion.div>
  );
}

export { getMonday };
