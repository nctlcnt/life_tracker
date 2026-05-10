import { useState, useEffect } from 'react';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from './ui/tooltip';

interface HeatmapData {
  projects: string[];
  dates: string[];
  data: Record<string, Record<string, number>>;
  archived?: string[];
}

const FOCUS_COLOR = '#94A3B5'; // 烟蓝灰

function minutesToOpacity(minutes: number, maxMinutes: number): number {
  if (minutes <= 0 || maxMinutes <= 0) return 0;
  // 使用对数曲线，让少量时间也有可见度，大量时间趋近于1
  return Math.min(1, 0.12 + 0.88 * Math.sqrt(minutes / maxMinutes));
}

function fmtDate(dateStr: string): string {
  const d = new Date(dateStr + 'T00:00:00');
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function fmtMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return `${m}分钟`;
  if (m === 0) return `${h}小时`;
  return `${h}小时${m}分钟`;
}

export function ProjectOverview() {
  const [data, setData] = useState<HeatmapData | null>(null);
  const [loading, setLoading] = useState(true);
  const [showArchived, setShowArchived] = useState(false);
  const [busyName, setBusyName] = useState<string | null>(null);

  const reload = () => {
    fetch('/api/projects/heatmap?days=90')
      .then(r => r.json())
      .then(d => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => {
    reload();
  }, []);

  const toggleArchive = async (name: string, archived: boolean) => {
    setBusyName(name);
    try {
      const url = archived ? '/api/projects/unarchive' : '/api/projects/archive';
      await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      reload();
    } finally {
      setBusyName(null);
    }
  };

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <span className="text-xs text-muted-foreground">加载中…</span>
      </div>
    );
  }

  const archivedSet = new Set<string>(data?.archived ?? []);
  const visibleProjects = (data?.projects ?? []).filter(
    p => showArchived || !archivedSet.has(p)
  );
  const archivedCount = (data?.projects ?? []).filter(p => archivedSet.has(p)).length;

  if (!data || data.projects.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center p-8">
        <div className="grid grid-cols-13 gap-1 opacity-20">
          {Array.from({ length: 91 }).map((_, i) => (
            <div
              key={i}
              className="w-3.5 h-3.5 rounded-sm"
              style={{ backgroundColor: FOCUS_COLOR }}
            />
          ))}
        </div>
        <p className="text-sm text-muted-foreground">
          暂无 Focus 数据
          <br />
          <span className="text-xs opacity-60">开始记录后，项目热力图将在这里显示</span>
        </p>
      </div>
    );
  }

  // 最大日投入分钟数（用于颜色深度归一化）
  // 用全量项目算，避免切换"显示已归档"时颜色基准跳动
  let globalMax = 0;
  for (const proj of data.projects) {
    for (const d of data.dates) {
      const v = data.data[proj]?.[d] ?? 0;
      if (v > globalMax) globalMax = v;
    }
  }

  // 按7天分组（每列是一周）
  const weeks: string[][] = [];
  for (let i = 0; i < data.dates.length; i += 7) {
    weeks.push(data.dates.slice(i, i + 7));
  }

  // 月份标签（每月第一天所在的列索引）
  const monthLabels: { weekIdx: number; label: string }[] = [];
  let lastMonth = -1;
  weeks.forEach((week, wi) => {
    week.forEach(d => {
      const m = new Date(d + 'T00:00:00').getMonth();
      if (m !== lastMonth) {
        monthLabels.push({ weekIdx: wi, label: `${m + 1}月` });
        lastMonth = m;
      }
    });
  });

  const PROJ_LABEL_W = 120; // 项目名列宽 px
  const CELL_SIZE = 14;     // 格子大小 px
  const CELL_GAP = 2;       // 格子间距 px

  return (
    <TooltipProvider delayDuration={80}>
      <div className="flex-1 overflow-auto px-8 py-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-sm font-medium text-foreground">Project Overview</h2>
          {archivedCount > 0 && (
            <button
              type="button"
              onClick={() => setShowArchived(s => !s)}
              className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
            >
              {showArchived ? `隐藏已归档 (${archivedCount})` : `显示已归档 (${archivedCount})`}
            </button>
          )}
        </div>

        <div className="overflow-x-auto">
          <div style={{ minWidth: PROJ_LABEL_W + weeks.length * (CELL_SIZE + CELL_GAP) }}>
            {/* 月份标签行 */}
            <div className="flex mb-1" style={{ paddingLeft: PROJ_LABEL_W }}>
              {weeks.map((_, wi) => {
                const label = monthLabels.find(ml => ml.weekIdx === wi);
                return (
                  <div
                    key={wi}
                    className="text-[10px] text-muted-foreground/70 flex-shrink-0"
                    style={{ width: CELL_SIZE + CELL_GAP }}
                  >
                    {label ? label.label : ''}
                  </div>
                );
              })}
            </div>

            {/* 项目行 */}
            {visibleProjects.map(proj => {
              const totalMinutes = Object.values(data.data[proj] ?? {}).reduce((a, b) => a + b, 0);
              const isArchived = archivedSet.has(proj);
              const isBusy = busyName === proj;
              return (
                <div
                  key={proj}
                  className="group flex items-center mb-1.5"
                  style={{ opacity: isArchived ? 0.45 : 1 }}
                >
                  {/* 项目名称 + 归档按钮 */}
                  <div
                    className="flex-shrink-0 flex items-center justify-end pr-3 text-xs text-foreground"
                    style={{ width: PROJ_LABEL_W }}
                    title={proj}
                  >
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => toggleArchive(proj, isArchived)}
                      title={isArchived ? '取消归档' : '归档'}
                      className="text-[10px] text-muted-foreground/60 hover:text-foreground opacity-0 group-hover:opacity-100 transition-opacity mr-1.5 px-1 py-0.5 rounded disabled:opacity-30"
                    >
                      {isArchived ? '↺' : '×'}
                    </button>
                    <span className="text-[11px] truncate">{proj}</span>
                    <span className="text-[10px] text-muted-foreground ml-1.5 flex-shrink-0">
                      {fmtMinutes(totalMinutes)}
                    </span>
                  </div>

                  {/* 热力格子 */}
                  <div className="flex">
                    {weeks.map((week, wi) => (
                      <div key={wi} className="flex flex-col" style={{ gap: CELL_GAP }}>
                        {week.map(dateStr => {
                          const minutes = data.data[proj]?.[dateStr] ?? 0;
                          const opacity = minutesToOpacity(minutes, globalMax);
                          return (
                            <Tooltip key={dateStr}>
                              <TooltipTrigger asChild>
                                <div
                                  className="rounded-[2px] cursor-default flex-shrink-0"
                                  style={{
                                    width: CELL_SIZE,
                                    height: CELL_SIZE,
                                    backgroundColor: FOCUS_COLOR,
                                    opacity: minutes > 0 ? opacity : 0.06,
                                    marginRight: CELL_GAP,
                                  }}
                                />
                              </TooltipTrigger>
                              {minutes > 0 && (
                                <TooltipContent
                                  side="top"
                                  sideOffset={4}
                                  className="bg-card text-foreground border border-border shadow-md px-2.5 py-1.5 rounded-md"
                                >
                                  <div className="text-[11px]">
                                    <span className="text-muted-foreground">{fmtDate(dateStr)} · </span>
                                    {fmtMinutes(minutes)}
                                  </div>
                                </TooltipContent>
                              )}
                            </Tooltip>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </TooltipProvider>
  );
}
