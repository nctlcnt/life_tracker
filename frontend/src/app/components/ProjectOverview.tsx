import { useState, useEffect } from 'react';
import type { FormEvent } from 'react';
import { Archive, Check, Pencil, Plus, RotateCcw, Trash2, X } from 'lucide-react';
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
  metric?: 'check_ins';
}

const FOCUS_COLOR = '#94A3B5'; // 烟蓝灰

function countToOpacity(count: number, maxCount: number): number {
  if (count <= 0 || maxCount <= 0) return 0;
  // 类 GitHub 热力图：少量 check-in 也可见，高频逐渐加深。
  return Math.min(1, 0.18 + 0.82 * Math.sqrt(count / maxCount));
}

function fmtDate(dateStr: string): string {
  const d = new Date(dateStr + 'T00:00:00');
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function fmtCheckIns(count: number): string {
  return `${count}次`;
}

export function ProjectOverview() {
  const [data, setData] = useState<HeatmapData | null>(null);
  const [loading, setLoading] = useState(true);
  const [showArchived, setShowArchived] = useState(false);
  const [busyName, setBusyName] = useState<string | null>(null);
  const [newProjectName, setNewProjectName] = useState('');
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState('');
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    fetch('/api/projects/heatmap?days=90')
      .then(r => r.json())
      .then(d => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  };

  const requestProject = async (url: string, options: RequestInit) => {
    const res = await fetch(url, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers ?? {}),
      },
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail || `Request failed: ${res.status}`);
    }
  };

  const createProject = async (event: FormEvent) => {
    event.preventDefault();
    const name = newProjectName.trim();
    if (!name) return;
    setBusyName(name);
    setError(null);
    try {
      await requestProject('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ name }),
      });
      setNewProjectName('');
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建项目失败');
    } finally {
      setBusyName(null);
    }
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

  const beginRename = (name: string) => {
    setEditingName(name);
    setEditingValue(name);
    setError(null);
  };

  const renameProject = async (oldName: string) => {
    const name = editingValue.trim();
    if (!name || name === oldName) {
      setEditingName(null);
      return;
    }
    setBusyName(oldName);
    setError(null);
    try {
      await requestProject(`/api/projects/${encodeURIComponent(oldName)}`, {
        method: 'PATCH',
        body: JSON.stringify({ name }),
      });
      setEditingName(null);
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : '重命名项目失败');
    } finally {
      setBusyName(null);
    }
  };

  const deleteProject = async (name: string) => {
    if (!window.confirm(`删除项目「${name}」？历史记录不会被删除，但 AI 之后看不到这个项目。`)) {
      return;
    }
    setBusyName(name);
    setError(null);
    try {
      await requestProject(`/api/projects/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      });
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除项目失败');
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

  // 最大日 check-in 次数（用于颜色深度归一化）
  // 用全量项目算，避免切换"显示已归档"时颜色基准跳动
  let globalMax = 0;
  for (const proj of data?.projects ?? []) {
    for (const d of data?.dates ?? []) {
      const v = data?.data[proj]?.[d] ?? 0;
      if (v > globalMax) globalMax = v;
    }
  }

  // 按7天分组（每列是一周）
  const weeks: string[][] = [];
  for (let i = 0; i < (data?.dates.length ?? 0); i += 7) {
    weeks.push((data?.dates ?? []).slice(i, i + 7));
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

  const PROJ_LABEL_W = 220; // 项目名列宽 px
  const CELL_SIZE = 14;     // 格子大小 px
  const CELL_GAP = 2;       // 格子间距 px

  return (
    <TooltipProvider delayDuration={80}>
      <div className="flex-1 overflow-auto px-8 py-6">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
          <h2 className="text-sm font-medium text-foreground">Project Overview</h2>
          <form onSubmit={createProject} className="flex items-center gap-2">
            <input
              type="text"
              value={newProjectName}
              onChange={event => setNewProjectName(event.target.value)}
              placeholder="New project"
              className="h-8 w-44 rounded-md border border-border bg-input-background px-2.5 text-xs outline-none focus:border-border-strong"
            />
            <button
              type="submit"
              disabled={!newProjectName.trim() || busyName === newProjectName.trim()}
              title="添加项目"
              className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border text-muted-foreground hover:text-foreground disabled:opacity-35"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
            {archivedCount > 0 && (
              <button
                type="button"
                onClick={() => setShowArchived(s => !s)}
                className="h-8 px-2 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
              >
                {showArchived ? `隐藏已归档 (${archivedCount})` : `显示已归档 (${archivedCount})`}
              </button>
            )}
          </form>
        </div>

        {error && (
          <div className="mb-4 text-xs text-destructive">{error}</div>
        )}

        {!data || data.projects.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-3 text-center p-8">
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
              暂无项目
              <br />
              <span className="text-xs opacity-60">先添加项目，AI 才能把 Focus 记录归到项目里</span>
            </p>
          </div>
        ) : (
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
              const totalCheckIns = Object.values(data.data[proj] ?? {}).reduce((a, b) => a + b, 0);
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
                    className="flex-shrink-0 flex items-center justify-end gap-1 pr-3 text-xs text-foreground"
                    style={{ width: PROJ_LABEL_W }}
                    title={proj}
                  >
                    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        type="button"
                        disabled={isBusy}
                        onClick={() => toggleArchive(proj, isArchived)}
                        title={isArchived ? '取消归档' : '归档'}
                        className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground/70 hover:text-foreground disabled:opacity-30"
                      >
                        {isArchived ? <RotateCcw className="h-3 w-3" /> : <Archive className="h-3 w-3" />}
                      </button>
                      <button
                        type="button"
                        disabled={isBusy}
                        onClick={() => beginRename(proj)}
                        title="重命名"
                        className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground/70 hover:text-foreground disabled:opacity-30"
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button
                        type="button"
                        disabled={isBusy}
                        onClick={() => deleteProject(proj)}
                        title="删除项目"
                        className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground/70 hover:text-destructive disabled:opacity-30"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                    {editingName === proj ? (
                      <div className="flex min-w-0 flex-1 items-center gap-0.5">
                        <input
                          type="text"
                          value={editingValue}
                          onChange={event => setEditingValue(event.target.value)}
                          onKeyDown={event => {
                            if (event.key === 'Enter') renameProject(proj);
                            if (event.key === 'Escape') setEditingName(null);
                          }}
                          className="h-6 min-w-0 flex-1 rounded border border-border bg-input-background px-1.5 text-[11px] outline-none focus:border-border-strong"
                          autoFocus
                        />
                        <button
                          type="button"
                          onClick={() => renameProject(proj)}
                          title="保存"
                          className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:text-foreground"
                        >
                          <Check className="h-3 w-3" />
                        </button>
                        <button
                          type="button"
                          onClick={() => setEditingName(null)}
                          title="取消"
                          className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:text-foreground"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    ) : (
                      <span className="min-w-0 flex-1 whitespace-normal break-words text-right text-[11px] leading-tight">{proj}</span>
                    )}
                    <span className="text-[10px] text-muted-foreground ml-1.5 flex-shrink-0">
                      {fmtCheckIns(totalCheckIns)}
                    </span>
                  </div>

                  {/* 热力格子 */}
                  <div className="flex">
                    {weeks.map((week, wi) => (
                      <div key={wi} className="flex flex-col" style={{ gap: CELL_GAP }}>
                        {week.map(dateStr => {
                          const count = data.data[proj]?.[dateStr] ?? 0;
                          const opacity = countToOpacity(count, globalMax);
                          return (
                            <Tooltip key={dateStr}>
                              <TooltipTrigger asChild>
                                <div
                                  className="rounded-[2px] cursor-default flex-shrink-0"
                                  style={{
                                    width: CELL_SIZE,
                                    height: CELL_SIZE,
                                    backgroundColor: FOCUS_COLOR,
                                    opacity: count > 0 ? opacity : 0.06,
                                    marginRight: CELL_GAP,
                                  }}
                                />
                              </TooltipTrigger>
                              {count > 0 && (
                                <TooltipContent
                                  side="top"
                                  sideOffset={4}
                                  className="bg-card text-foreground border border-border shadow-md px-2.5 py-1.5 rounded-md"
                                >
                                  <div className="text-[11px]">
                                    <span className="text-muted-foreground">{fmtDate(dateStr)} · </span>
                                    {fmtCheckIns(count)} check-in
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
        )}
      </div>
    </TooltipProvider>
  );
}
