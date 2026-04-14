import { useMemo, useState } from 'react';
import { TimelineEvent } from './MultiLaneTimeline';

interface ChillDrainChartProps {
  events: TimelineEvent[];
}

const CHILL_COLOR = '#A8B9A0'; // 鼠尾草绿
const DRAIN_COLOR = '#D4AFA0'; // 灰粉
const FOCUS_COLOR = '#94A3B5'; // 烟蓝灰
const ROUTINE_COLOR = '#C4BFB0'; // 暖米灰

const fmtDuration = (minutes: number) => {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return `${m}分`;
  if (m === 0) return `${h}h`;
  return `${h}h${m}m`;
};

type FilterMode = 'all' | 'chill' | 'drain';

export function ChillDrainChart({ events }: ChillDrainChartProps) {
  const [filter, setFilter] = useState<FilterMode>('all');

  const stats = useMemo(() => {
    let chillMin = 0;
    let drainMin = 0;
    let focusMin = 0;
    let routineMin = 0;

    for (const ev of events) {
      const mins = (ev.endDate.getTime() - ev.startDate.getTime()) / 60000;
      if (mins <= 0) continue;

      if (ev.category === 'Focus') {
        focusMin += mins;
      } else if (ev.category === 'Routine') {
        routineMin += mins;
      } else if (ev.category === 'Chill') {
        if (ev.energy_type === 'drain') {
          drainMin += mins;
        } else {
          chillMin += mins; // null 也算蓄水
        }
      }
    }

    return { chillMin, drainMin, focusMin, routineMin };
  }, [events]);

  const { chillMin, drainMin, focusMin, routineMin } = stats;
  const totalChillDrain = chillMin + drainMin;
  const chillPct = totalChillDrain > 0 ? (chillMin / totalChillDrain) * 100 : 0;
  const drainPct = totalChillDrain > 0 ? (drainMin / totalChillDrain) * 100 : 0;

  // 筛选后展示的事件（仅用于显示筛选状态，实际图表始终显示总量）
  const hasData = totalChillDrain > 0 || focusMin > 0 || routineMin > 0;

  if (!hasData) {
    return (
      <div className="px-6 py-5 flex items-center justify-center">
        <span className="text-xs text-muted-foreground">暂无今日数据</span>
      </div>
    );
  }

  return (
    <div className="px-6 py-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-medium text-muted-foreground tracking-wide">精力分布</h3>

        {/* 筛选 chips */}
        {totalChillDrain > 0 && (
          <div className="flex items-center gap-1.5">
            {(['all', 'chill', 'drain'] as FilterMode[]).map(f => (
              <button
                key={f}
                onClick={() => setFilter(f === filter ? 'all' : f)}
                className={`px-2 py-0.5 rounded-full text-[10px] transition-all border ${
                  filter === f
                    ? 'border-transparent text-white'
                    : 'border-border text-muted-foreground bg-transparent hover:bg-muted'
                }`}
                style={filter === f ? {
                  backgroundColor: f === 'chill' ? CHILL_COLOR : f === 'drain' ? DRAIN_COLOR : 'var(--color-muted-foreground)',
                } : {}}
              >
                {f === 'all' ? '全部' : f === 'chill' ? '蓄水' : '漏水'}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2">
        {/* Focus 行 */}
        {focusMin > 0 && (filter === 'all') && (
          <div className="flex items-center gap-2">
            <span
              className="w-2 h-2 rounded-full flex-shrink-0"
              style={{ backgroundColor: FOCUS_COLOR }}
            />
            <span className="text-[11px] text-foreground w-12 flex-shrink-0">Focus</span>
            <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.min(100, (focusMin / Math.max(focusMin + routineMin + totalChillDrain, 1)) * 100)}%`,
                  backgroundColor: FOCUS_COLOR,
                  opacity: 0.85,
                }}
              />
            </div>
            <span className="text-[10px] text-muted-foreground tabular-nums w-10 text-right">
              {fmtDuration(focusMin)}
            </span>
          </div>
        )}

        {/* Routine 行 */}
        {routineMin > 0 && (filter === 'all') && (
          <div className="flex items-center gap-2">
            <span
              className="w-2 h-2 rounded-full flex-shrink-0"
              style={{ backgroundColor: ROUTINE_COLOR }}
            />
            <span className="text-[11px] text-foreground w-12 flex-shrink-0">Routine</span>
            <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.min(100, (routineMin / Math.max(focusMin + routineMin + totalChillDrain, 1)) * 100)}%`,
                  backgroundColor: ROUTINE_COLOR,
                  opacity: 0.85,
                }}
              />
            </div>
            <span className="text-[10px] text-muted-foreground tabular-nums w-10 text-right">
              {fmtDuration(routineMin)}
            </span>
          </div>
        )}

        {/* 蓄水/漏水区域 */}
        {totalChillDrain > 0 && (filter === 'all' || filter === 'chill' || filter === 'drain') && (
          <>
            {/* 蓄水行 */}
            {(filter === 'all' || filter === 'chill') && chillMin > 0 && (
              <div className="flex items-center gap-2">
                <span
                  className="w-2 h-2 rounded-full flex-shrink-0"
                  style={{ backgroundColor: CHILL_COLOR }}
                />
                <span className="text-[11px] text-foreground w-12 flex-shrink-0">蓄水</span>
                <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${chillPct}%`,
                      backgroundColor: CHILL_COLOR,
                      opacity: 0.85,
                    }}
                  />
                </div>
                <span className="text-[10px] text-muted-foreground tabular-nums w-10 text-right">
                  {fmtDuration(chillMin)}
                </span>
              </div>
            )}

            {/* 漏水行 */}
            {(filter === 'all' || filter === 'drain') && drainMin > 0 && (
              <div className="flex items-center gap-2">
                <span
                  className="w-2 h-2 rounded-full flex-shrink-0"
                  style={{ backgroundColor: DRAIN_COLOR }}
                />
                <span className="text-[11px] text-foreground w-12 flex-shrink-0">漏水</span>
                <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${drainPct}%`,
                      backgroundColor: DRAIN_COLOR,
                      opacity: 0.85,
                    }}
                  />
                </div>
                <span className="text-[10px] text-muted-foreground tabular-nums w-10 text-right">
                  {fmtDuration(drainMin)}
                </span>
              </div>
            )}

            {/* 蓄水/漏水总比例条（仅在显示 Chill 相关时展示） */}
            {chillMin > 0 && drainMin > 0 && (filter === 'all' || filter === 'chill' || filter === 'drain') && (
              <div className="mt-1 flex items-center gap-2">
                <span className="text-[10px] text-muted-foreground w-14 flex-shrink-0">今日比例</span>
                <div className="flex-1 h-2 rounded-full overflow-hidden flex">
                  <div
                    className="h-full rounded-l-full transition-all"
                    style={{ width: `${chillPct}%`, backgroundColor: CHILL_COLOR, opacity: 0.75 }}
                  />
                  <div
                    className="h-full rounded-r-full transition-all"
                    style={{ width: `${drainPct}%`, backgroundColor: DRAIN_COLOR, opacity: 0.75 }}
                  />
                </div>
                <span className="text-[10px] text-muted-foreground tabular-nums w-10 text-right">
                  {Math.round(chillPct)}%
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
