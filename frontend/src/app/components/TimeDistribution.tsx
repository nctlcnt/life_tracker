import { useMemo } from 'react';
import { PieChart, Pie, Cell, ResponsiveContainer } from 'recharts';

interface Task {
  id: string;
  name: string;
  category: string;
  startDate: Date;
  endDate: Date;
  color?: string;
}

interface TimeDistributionProps {
  tasks: Task[];
  catColors: Record<string, string>;
}

const fmtDuration = (minutes: number) => {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return `${m}分钟`;
  if (m === 0) return `${h}小时`;
  return `${h}小时${m}分钟`;
};

export function TimeDistribution({ tasks, catColors }: TimeDistributionProps) {
  const data = useMemo(() => {
    const categoryMinutes: Record<string, number> = {};

    for (const task of tasks) {
      const mins = (task.endDate.getTime() - task.startDate.getTime()) / 60000;
      if (mins <= 0) continue;
      const cat = task.category || 'uncategorized';
      categoryMinutes[cat] = (categoryMinutes[cat] || 0) + mins;
    }

    return Object.entries(categoryMinutes)
      .map(([category, minutes]) => ({
        category,
        minutes,
        color: catColors[category] || catColors['uncategorized'] || '#B5B1A8',
      }))
      .sort((a, b) => b.minutes - a.minutes);
  }, [tasks, catColors]);

  const totalMinutes = data.reduce((sum, d) => sum + d.minutes, 0);

  if (data.length === 0) {
    return (
      <div className="px-6 py-6 text-center text-sm text-muted-foreground">
        暂无时间数据
      </div>
    );
  }

  return (
    <div className="px-6 py-5">
      <h3 className="text-sm font-medium text-foreground mb-4">时间分布</h3>
      <div className="flex items-start gap-6">
        {/* Pie chart */}
        <div className="w-[120px] h-[120px] flex-shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                dataKey="minutes"
                cx="50%"
                cy="50%"
                innerRadius={30}
                outerRadius={55}
                strokeWidth={1}
                stroke="var(--color-background)"
              >
                {data.map((entry) => (
                  <Cell key={entry.category} fill={entry.color} />
                ))}
              </Pie>
            </PieChart>
          </ResponsiveContainer>
        </div>

        {/* Legend / breakdown */}
        <div className="flex-1 flex flex-col gap-2 min-w-0">
          {data.map((entry) => {
            const pct = totalMinutes > 0 ? (entry.minutes / totalMinutes * 100) : 0;
            return (
              <div key={entry.category} className="flex items-center gap-2">
                <span
                  className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                  style={{ backgroundColor: entry.color }}
                />
                <span className="text-sm text-foreground flex-shrink-0">
                  {entry.category}
                </span>
                <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden mx-1">
                  <div
                    className="h-full rounded-full transition-all"
                    style={{ width: `${pct}%`, backgroundColor: entry.color }}
                  />
                </div>
                <span className="text-xs text-muted-foreground tabular-nums flex-shrink-0 min-w-[60px] text-right">
                  {fmtDuration(entry.minutes)}
                </span>
              </div>
            );
          })}
          <div className="text-xs text-muted-foreground mt-1 pt-1 border-t border-border">
            总计 {fmtDuration(totalMinutes)}
          </div>
        </div>
      </div>
    </div>
  );
}
