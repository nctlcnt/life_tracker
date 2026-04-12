import { useState, useRef, useEffect } from 'react';
import { motion } from 'motion/react';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from './ui/tooltip';

interface GanttTask {
  id: string;
  name: string;
  category?: string;
  startDate: Date;
  endDate: Date;
  color?: string;
  notes?: string | null;
  row: number;
}

interface GanttChartProps {
  tasks: GanttTask[];
  startDate: Date;
  endDate: Date;
}

// 行高与条形高度 —— 保持细线条、简洁雅致
const ROW_HEIGHT = 30;
const BAR_HEIGHT = 16;
const BAR_OFFSET = (ROW_HEIGHT - BAR_HEIGHT) / 2;

const fmtTime = (d: Date) =>
  d.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });

const fmtDuration = (ms: number) => {
  const totalMin = Math.max(0, Math.round(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h === 0) return `${m} 分钟`;
  if (m === 0) return `${h} 小时`;
  return `${h} 小时 ${m} 分钟`;
};

export function GanttChart({ tasks, startDate, endDate }: GanttChartProps) {
  const [viewportWidth, setViewportWidth] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (containerRef.current) {
      setViewportWidth(containerRef.current.offsetWidth);
    }

    const handleResize = () => {
      if (containerRef.current) {
        setViewportWidth(containerRef.current.offsetWidth);
      }
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  const totalHours = Math.ceil(
    (endDate.getTime() - startDate.getTime()) / (1000 * 60 * 60)
  );
  const hourWidth = viewportWidth / totalHours;

  const getTaskPosition = (task: GanttTask) => {
    const taskStartHours = Math.max(
      0,
      (task.startDate.getTime() - startDate.getTime()) / (1000 * 60 * 60)
    );
    const taskDurationHours =
      (task.endDate.getTime() - task.startDate.getTime()) / (1000 * 60 * 60);

    return {
      left: taskStartHours * hourWidth,
      width: Math.max(taskDurationHours * hourWidth, 2),
    };
  };

  const maxRow = Math.max(...tasks.map((t) => t.row), 0);
  const timelineHeight = (maxRow + 1) * ROW_HEIGHT;

  const hours: { label: string; hour: number }[] = [];
  for (let i = 0; i < totalHours; i++) {
    const hourDate = new Date(startDate.getTime() + i * 60 * 60 * 1000);
    hours.push({
      label: hourDate.toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
      }),
      hour: i,
    });
  }

  return (
    <TooltipProvider delayDuration={120}>
      <div className="bg-background">
        <div ref={containerRef} className="overflow-x-auto">
          <div style={{ minWidth: totalHours * 60 }}>
            {/* 时间刻度 */}
            <div className="flex">
              {hours.map((hour, idx) => (
                <div
                  key={idx}
                  className="px-2 py-1.5 text-[10px] tracking-wide text-muted-foreground/70 flex-shrink-0"
                  style={{ width: hourWidth, minWidth: 60 }}
                >
                  {hour.label}
                </div>
              ))}
            </div>

            {/* 时间线主体 */}
            <div
              className="relative"
              style={{ height: timelineHeight, paddingBottom: 4 }}
            >
              {/* 竖向分隔线 —— 极淡 */}
              {Array.from({ length: totalHours }).map((_, i) => (
                <div
                  key={i}
                  className="absolute top-0 bottom-0"
                  style={{
                    left: i * hourWidth,
                    width: 1,
                    backgroundColor: 'rgba(0,0,0,0.04)',
                  }}
                />
              ))}

              {/* 行基线 —— 极细、极淡 */}
              {Array.from({ length: maxRow + 1 }).map((_, i) => (
                <div
                  key={`row-${i}`}
                  className="absolute left-0 right-0"
                  style={{
                    top: i * ROW_HEIGHT + ROW_HEIGHT / 2,
                    height: 1,
                    backgroundColor: 'rgba(0,0,0,0.035)',
                  }}
                />
              ))}

              {/* 事件条 */}
              {tasks.map((task) => {
                const pos = getTaskPosition(task);
                const durationMs =
                  task.endDate.getTime() - task.startDate.getTime();
                return (
                  <Tooltip key={task.id}>
                    <TooltipTrigger asChild>
                      <motion.div
                        className="absolute rounded-[3px] px-2 text-[10px] cursor-pointer overflow-hidden hover:brightness-95 hover:shadow-sm transition-all"
                        style={{
                          left: pos.left,
                          width: pos.width,
                          top: task.row * ROW_HEIGHT + BAR_OFFSET,
                          height: BAR_HEIGHT,
                          lineHeight: `${BAR_HEIGHT}px`,
                          backgroundColor:
                            task.color || 'var(--color-primary)',
                          color: 'rgba(40,40,40,0.78)',
                        }}
                        initial={{ opacity: 0, y: -4 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{
                          duration: 0.25,
                          delay: task.row * 0.03,
                        }}
                      >
                        <div className="truncate">{task.name}</div>
                      </motion.div>
                    </TooltipTrigger>
                    <TooltipContent
                      side="top"
                      sideOffset={6}
                      className="bg-card text-foreground border border-border shadow-md px-3 py-2 rounded-md max-w-xs"
                    >
                      <div className="flex flex-col gap-1">
                        <div className="flex items-center gap-2">
                          <span
                            className="inline-block w-2 h-2 rounded-full flex-shrink-0"
                            style={{
                              backgroundColor:
                                task.color || 'var(--color-primary)',
                            }}
                          />
                          <span className="text-xs font-medium leading-snug">
                            {task.name}
                          </span>
                        </div>
                        {task.category && (
                          <div className="text-[10px] text-muted-foreground pl-4">
                            {task.category}
                          </div>
                        )}
                        {task.notes && (
                          <div className="text-[10px] text-muted-foreground pl-4 leading-relaxed whitespace-pre-line">
                            {task.notes}
                          </div>
                        )}
                        <div className="text-[10px] text-muted-foreground pl-4 tabular-nums">
                          {fmtTime(task.startDate)} — {fmtTime(task.endDate)}
                          <span className="mx-1.5 opacity-50">·</span>
                          {fmtDuration(durationMs)}
                        </div>
                      </div>
                    </TooltipContent>
                  </Tooltip>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </TooltipProvider>
  );
}
