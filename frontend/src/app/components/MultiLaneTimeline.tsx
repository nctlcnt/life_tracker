import { useRef, useEffect, useState } from 'react';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from './ui/tooltip';

// ── 类型 ────────────────────────────────────────────────────────
export interface TimelineEvent {
  id: string;
  content: string;
  category: 'Focus' | 'Routine' | 'Chill' | string;
  startDate: Date;
  endDate: Date;
  notes?: string | null;
  project_name?: string | null;
}

interface MultiLaneTimelineProps {
  events: TimelineEvent[];
  date: string; // YYYY-MM-DD
}

// ── 色彩常量 ──────────────────────────────────────────────────
const LANE_COLORS: Record<string, string> = {
  Focus: '#94A3B5',   // 烟蓝灰
  Routine: '#C4BFB0', // 暖米灰
  Chill: '#A8B9A0',   // 鼠尾草绿
  // 旧分类兜底
  '休息': '#C4BFB0', '工作': '#94A3B5', '社交': '#A8B9A0',
  '生活': '#D4AFA0', '健康': '#9FBAB0', '娱乐': '#B5A6BC',
  '出行': '#CFB897', 'uncategorized': '#B5B1A8',
};

const LANES = ['Focus', 'Routine', 'Chill'] as const;

const LANE_LABELS: Record<string, string> = {
  Focus: 'Focus',
  Routine: 'Rtn',
  Chill: 'Chill',
};

// ── 工具函数 ──────────────────────────────────────────────────
const fmtTime = (d: Date) =>
  d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });

const fmtDuration = (ms: number) => {
  const totalMin = Math.max(0, Math.round(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h === 0) return `${m}分`;
  if (m === 0) return `${h}h`;
  return `${h}h${m}m`;
};

// 时间轴：显示范围 06:00 - 翌日 02:00（20小时，覆盖绝大多数生活节奏）
const DAY_START_HOUR = 6;   // 06:00
const DAY_SPAN_HOURS = 20;  // 到凌晨 02:00

function timeToRatio(date: Date, dayBase: Date): number {
  const baseMs = dayBase.getTime() + DAY_START_HOUR * 3600 * 1000;
  const spanMs = DAY_SPAN_HOURS * 3600 * 1000;
  return Math.min(1, Math.max(0, (date.getTime() - baseMs) / spanMs));
}

// ── 组件 ─────────────────────────────────────────────────────
export function MultiLaneTimeline({ events, date }: MultiLaneTimelineProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(0);

  useEffect(() => {
    const update = () => {
      if (containerRef.current) setHeight(containerRef.current.offsetHeight);
    };
    update();
    const ro = new ResizeObserver(update);
    if (containerRef.current) ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  const dayBase = new Date(`${date}T00:00:00`);
  const now = new Date();

  // 当前时间线位置
  const nowRatio = timeToRatio(now, dayBase);
  const showNowLine = nowRatio > 0 && nowRatio < 1;

  // 时刻刻度（每2小时一条）
  const tickHours: number[] = [];
  for (let h = DAY_START_HOUR; h <= DAY_START_HOUR + DAY_SPAN_HOURS; h += 2) {
    tickHours.push(h);
  }

  // 将事件按 category 分配到泳道
  const laneEvents: Record<string, TimelineEvent[]> = { Focus: [], Routine: [], Chill: [] };
  for (const ev of events) {
    const lane = LANES.includes(ev.category as any) ? ev.category : null;
    if (lane && laneEvents[lane]) laneEvents[lane].push(ev);
  }

  const tickAreaWidth = 26; // px，左侧时刻标注宽度

  return (
    <TooltipProvider delayDuration={100}>
      <div className="flex flex-col h-full px-2 pt-3 pb-2 select-none">
        {/* 泳道标题 */}
        <div className="flex mb-1" style={{ paddingLeft: tickAreaWidth }}>
          {LANES.map(lane => (
            <div
              key={lane}
              className="flex-1 text-center text-[10px] font-medium tracking-wide"
              style={{ color: LANE_COLORS[lane] }}
            >
              {LANE_LABELS[lane]}
            </div>
          ))}
        </div>

        {/* 时间轴主体 */}
        <div ref={containerRef} className="flex flex-1 min-h-0 relative">
          {/* 左侧时刻刻度 */}
          <div
            className="flex-shrink-0 relative"
            style={{ width: tickAreaWidth }}
          >
            {height > 0 && tickHours.map(h => {
              const ratio = (h - DAY_START_HOUR) / DAY_SPAN_HOURS;
              return (
                <div
                  key={h}
                  className="absolute right-1 text-[8px] text-muted-foreground/60 leading-none"
                  style={{ top: ratio * height - 4 }}
                >
                  {String(h % 24).padStart(2, '0')}
                </div>
              );
            })}
          </div>

          {/* 三条泳道 */}
          <div className="flex flex-1 gap-0.5 relative">
            {/* 横向时刻分割线 */}
            {height > 0 && tickHours.map(h => {
              const ratio = (h - DAY_START_HOUR) / DAY_SPAN_HOURS;
              return (
                <div
                  key={h}
                  className="absolute left-0 right-0 pointer-events-none"
                  style={{
                    top: ratio * height,
                    height: 1,
                    backgroundColor: 'rgba(0,0,0,0.04)',
                    zIndex: 0,
                  }}
                />
              );
            })}

            {/* 当前时间线 */}
            {showNowLine && height > 0 && (
              <div
                className="absolute left-0 right-0 pointer-events-none z-10"
                style={{
                  top: nowRatio * height,
                  height: 1.5,
                  backgroundColor: 'rgba(180,80,80,0.5)',
                }}
              />
            )}

            {LANES.map(lane => (
              <div key={lane} className="flex-1 relative rounded-sm overflow-hidden">
                {/* 泳道背景 */}
                <div
                  className="absolute inset-0 rounded-sm"
                  style={{ backgroundColor: 'rgba(0,0,0,0.02)' }}
                />

                {/* 事件块 */}
                {height > 0 && laneEvents[lane].map(ev => {
                  const topRatio = timeToRatio(ev.startDate, dayBase);
                  const botRatio = timeToRatio(ev.endDate, dayBase);
                  const h = Math.max((botRatio - topRatio) * height, 3);
                  const color = LANE_COLORS[lane] || LANE_COLORS.uncategorized;
                  const durationMs = ev.endDate.getTime() - ev.startDate.getTime();

                  return (
                    <Tooltip key={ev.id}>
                      <TooltipTrigger asChild>
                        <div
                          className="absolute left-0.5 right-0.5 rounded-[2px] cursor-pointer hover:brightness-95 transition-all overflow-hidden"
                          style={{
                            top: topRatio * height,
                            height: h,
                            backgroundColor: color,
                            opacity: 0.85,
                          }}
                        >
                          {h > 18 && (
                            <div
                              className="px-1 text-[8px] leading-tight truncate"
                              style={{ color: 'rgba(40,40,40,0.7)', paddingTop: 2 }}
                            >
                              {ev.project_name || ev.content}
                            </div>
                          )}
                        </div>
                      </TooltipTrigger>
                      <TooltipContent
                        side="right"
                        sideOffset={6}
                        className="bg-card text-foreground border border-border shadow-md px-3 py-2 rounded-md max-w-[200px]"
                      >
                        <div className="flex flex-col gap-1">
                          <div className="flex items-center gap-1.5">
                            <span
                              className="w-2 h-2 rounded-full flex-shrink-0"
                              style={{ backgroundColor: color }}
                            />
                            <span className="text-xs font-medium leading-snug">{ev.content}</span>
                          </div>
                          {ev.project_name && (
                            <div className="text-[10px] text-muted-foreground pl-3.5">{ev.project_name}</div>
                          )}
                          {ev.notes && (
                            <div className="text-[10px] text-muted-foreground pl-3.5 leading-relaxed whitespace-pre-line line-clamp-3">
                              {ev.notes}
                            </div>
                          )}
                          <div className="text-[10px] text-muted-foreground pl-3.5 tabular-nums">
                            {fmtTime(ev.startDate)} — {fmtTime(ev.endDate)}
                            <span className="mx-1 opacity-50">·</span>
                            {fmtDuration(durationMs)}
                          </div>
                        </div>
                      </TooltipContent>
                    </Tooltip>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      </div>
    </TooltipProvider>
  );
}
