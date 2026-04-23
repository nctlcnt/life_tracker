import { useEffect, useMemo, useRef, useState } from 'react';
import './rhythm.css';

// ── Types ─────────────────────────────────────────────────────────
type Track = 'chill' | 'focus' | 'routine';
type BlockKind = 'logged' | 'intent';

interface Block {
  id: string;
  track: Track;
  title: string;
  start: number; // decimal hours, relative to dayBase (today 00:00)
  end: number;
  note: string;
  kind: BlockKind;
  project?: string;
  isOngoing?: boolean;
}

interface PlannedItem {
  id: string;
  start: number;
  end: number;
  startIso: string;
  track: Track;
  title: string;
  note: string;
  project?: string;
}

interface ProjectRow {
  name: string;
  hours: number;
}

// ── Constants ─────────────────────────────────────────────────────
const TRACKS: Record<Track, { label: string }> = {
  chill: { label: 'Chill' },
  focus: { label: 'Focus' },
  routine: { label: 'Routine' },
};

const DAY_START_HOUR = 6;
const DAY_END_HOUR = 24;

const PROJECT_TONES = [
  'oklch(55% 0.12 40)',
  'oklch(50% 0.10 240)',
  'oklch(52% 0.12 160)',
  'oklch(45% 0.06 310)',
  'oklch(55% 0.10 110)',
  'oklch(50% 0.12 30)',
  'oklch(48% 0.09 210)',
  'oklch(54% 0.11 80)',
];

// ── Helpers ───────────────────────────────────────────────────────
const pad2 = (n: number) => String(n).padStart(2, '0');

const fmtDateStr = (d: Date) =>
  `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;

const fmtTime = (h: number) => {
  // h can be negative or > 24; normalize for display
  const whole = Math.floor(h);
  const mm = Math.round((h - whole) * 60);
  return `${pad2(((whole % 24) + 24) % 24)}:${pad2(mm)}`;
};

const fmtDur = (mins: number) => {
  if (mins < 60) return `${Math.max(0, Math.round(mins))}m`;
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
};

const categoryToTrack = (cat: string | undefined | null): Track | null => {
  if (cat === 'Focus') return 'focus';
  if (cat === 'Chill') return 'chill';
  if (cat === 'Routine') return 'routine';
  return null;
};

const toDayHours = (d: Date, dayBase: Date) =>
  (d.getTime() - dayBase.getTime()) / (3600 * 1000);

const hashCode = (s: string) => {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
};

const projectTone = (name: string) =>
  PROJECT_TONES[hashCode(name) % PROJECT_TONES.length];

// Day name like THU · 04/23
const DAY_ABBR = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
const fmtRailDate = (d: Date) =>
  `${DAY_ABBR[d.getDay()]} · ${pad2(d.getMonth() + 1)}/${pad2(d.getDate())}`;

// ── Energy Bar (24h mock) ─────────────────────────────────────────
function EnergyBar({ nowHour }: { nowHour: number }) {
  const capacity = 24;
  const spent = Math.max(0, Math.min(capacity, nowHour));
  const remaining = capacity - spent;
  const pctSpent = (spent / capacity) * 100;

  return (
    <div className="rhythm-energy">
      <div className="rhythm-energy-head">
        <div className="rhythm-energy-title">
          <span className="rhythm-kicker">ENERGY · 今日已消耗</span>
          <span className="rhythm-big-num">
            {spent.toFixed(1)}
            <span className="rhythm-big-num-unit">/{capacity}h</span>
          </span>
          <span className="rhythm-energy-sub">
            剩余 <b>{remaining.toFixed(1)}h</b> · mock 时间占位，精力系统稍后接入
          </span>
        </div>
      </div>

      <div className="rhythm-energy-bar">
        <div
          className="rhythm-eb-seg rhythm-eb-spent"
          style={{ width: `${pctSpent}%` }}
        />
        <div className="rhythm-eb-ticks">
          {Array.from({ length: 11 }).map((_, i) => (
            <span key={i} style={{ left: `${i * 10}%` }} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Timeline Rail ─────────────────────────────────────────────────
function TimelineRail({
  blocks,
  nowHour,
  activeId,
  onPick,
  railDate,
  startH = DAY_START_HOUR,
  endH = DAY_END_HOUR,
}: {
  blocks: Block[];
  nowHour: number;
  activeId: string | null;
  onPick: (id: string) => void;
  railDate: Date;
  startH?: number;
  endH?: number;
}) {
  const hours = endH - startH;
  const trackOrder: Track[] = ['chill', 'focus', 'routine'];
  const railRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!railRef.current) return;
    const el = railRef.current;
    const y = ((nowHour - startH) / hours) * el.scrollHeight - el.clientHeight / 2;
    el.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
  }, [nowHour, startH, hours]);

  const visibleBlocks = blocks.filter((b) => b.end > startH && b.start < endH);

  return (
    <div className="rhythm-rail" ref={railRef}>
      <div className="rhythm-rail-head">
        <div className="rhythm-rail-head-row">
          <span className="rhythm-rail-head-label">Timeline</span>
          <span className="rhythm-rail-head-date">{fmtRailDate(railDate)}</span>
        </div>
        <div className="rhythm-rail-tracks">
          {trackOrder.map((t) => (
            <div key={t} className={`rhythm-rt rhythm-rt-${t}`}>
              <i />
              <span>{TRACKS[t].label}</span>
            </div>
          ))}
        </div>
      </div>

      <div
        className="rhythm-rail-body"
        style={{ ['--rhythm-hours' as string]: hours }}
      >
        <div className="rhythm-hours">
          {Array.from({ length: hours + 1 }).map((_, i) => {
            const h = startH + i;
            return (
              <div
                key={h}
                className="rhythm-hour"
                style={{ top: `calc(${i} * var(--rhythm-hour-h))` }}
              >
                <span className="rhythm-hour-lbl">{pad2(h % 24)}</span>
                <span className="rhythm-hour-line" />
              </div>
            );
          })}
        </div>

        <div className="rhythm-cols">
          {trackOrder.map((t) => (
            <div key={t} className={`rhythm-col rhythm-col-${t}`} />
          ))}
        </div>

        <div className="rhythm-blocks">
          {visibleBlocks.map((b) => {
            const colIdx = trackOrder.indexOf(b.track);
            const clampedStart = Math.max(startH, b.start);
            const clampedEnd = Math.min(endH, b.end);
            const top = ((clampedStart - startH) / hours) * 100;
            const height = Math.max(
              0.5,
              ((clampedEnd - clampedStart) / hours) * 100,
            );
            const isIntent = b.kind === 'intent';
            const isNow = b.isOngoing || (nowHour >= b.start && nowHour < b.end && !isIntent);
            const durMins = Math.round((b.end - b.start) * 60);
            return (
              <button
                key={`${b.kind}-${b.id}`}
                onClick={() => onPick(b.id)}
                className={[
                  'rhythm-blk',
                  `rhythm-blk-${b.track}`,
                  isIntent ? 'is-intent' : 'is-logged',
                  isNow ? 'is-now' : '',
                  activeId === b.id ? 'is-active' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                style={{
                  top: `${top}%`,
                  height: `${height}%`,
                  left: `calc(${colIdx} * (100%/3))`,
                  width: 'calc(100%/3)',
                }}
                title={b.title}
              >
                <div className="rhythm-blk-time">
                  {fmtTime(b.start)}
                  {isIntent && <span className="rhythm-blk-intent-tag"> · 计划</span>}
                </div>
                <div className="rhythm-blk-title">
                  {b.project ? `${b.project} · ` : ''}
                  {b.title}
                </div>
                <div className="rhythm-blk-meta">
                  <span className="rhythm-blk-dur">
                    {b.isOngoing ? '进行中' : fmtDur(durMins)}
                  </span>
                </div>
                {isNow && <span className="rhythm-blk-pulse" />}
              </button>
            );
          })}
        </div>

        <div
          className="rhythm-nowline"
          style={{ top: `${((nowHour - startH) / hours) * 100}%` }}
        >
          <span className="rhythm-nowline-dot" />
          <span className="rhythm-nowline-lbl">{fmtTime(nowHour)}</span>
        </div>
      </div>
    </div>
  );
}

// ── Now Card ──────────────────────────────────────────────────────
function NowCard({ block, nowHour }: { block: Block | null; nowHour: number }) {
  if (!block) {
    return (
      <div className="rhythm-now-card rhythm-now-card--empty">
        <span className="rhythm-kicker">正在记录 · 当前</span>
        <h2>空档 · 没有正在进行的事件</h2>
        <p>AI 尚未捕获正在进行的活动。下一条开启的记录会自动出现在这里。</p>
      </div>
    );
  }
  const elapsedMins = Math.max(0, Math.round((nowHour - block.start) * 60));
  return (
    <div className={`rhythm-now-card rhythm-now-card--${block.track}`}>
      <div className="rhythm-now-card-head">
        <span className="rhythm-kicker">
          正在记录 · {TRACKS[block.track].label}
          {block.project ? ` · ${block.project}` : ''}
        </span>
        <span className="rhythm-now-card-time rhythm-mono">
          自 {fmtTime(block.start)} · 已 {fmtDur(elapsedMins)}
        </span>
      </div>
      <h2 className="rhythm-now-card-title">{block.title}</h2>
      {block.note ? (
        <p className="rhythm-now-card-note" style={{ whiteSpace: 'pre-line' }}>
          {block.note}
        </p>
      ) : (
        <p className="rhythm-now-card-note" style={{ opacity: 0.55 }}>
          （暂无备注）
        </p>
      )}
      <div className="rhythm-now-indicator">
        <span className="rhythm-ni-pulse" />
        <span className="rhythm-ni-label rhythm-mono">LIVE · 由 AI 自动写入</span>
      </div>
    </div>
  );
}

// ── Projects Summary (weekly) ─────────────────────────────────────
function ProjectsSummary({ rows }: { rows: ProjectRow[] }) {
  const totalHours = rows.reduce((s, r) => s + r.hours, 0);
  const maxHours = Math.max(0.0001, ...rows.map((r) => r.hours));

  return (
    <div className="rhythm-ov-projects">
      <div className="rhythm-ov-proj-head">
        <span className="rhythm-kicker">PROJECTS · Focus 分解（近 7 天）</span>
        <span className="rhythm-ov-proj-total">{totalHours.toFixed(1)}h</span>
      </div>
      <div className="rhythm-ov-proj-list">
        {rows.map((r) => {
          const tone = projectTone(r.name);
          const wPct = (r.hours / maxHours) * 100;
          return (
            <div key={r.name} className="rhythm-ov-proj-row">
              <div className="rhythm-ov-proj-top">
                <span
                  className="rhythm-ov-proj-dot"
                  style={{ background: tone }}
                />
                <span className="rhythm-ov-proj-name">{r.name}</span>
                <span className="rhythm-ov-proj-hrs rhythm-mono">
                  {r.hours.toFixed(1)}h
                </span>
              </div>
              <div className="rhythm-ov-proj-bar" style={{ width: `${wPct}%` }}>
                <div
                  className="rhythm-ov-proj-fill"
                  style={{ width: '100%', background: tone }}
                />
              </div>
            </div>
          );
        })}
        {rows.length === 0 && (
          <div className="rhythm-ov-proj-empty">近 7 天无 Focus 项目记录。</div>
        )}
      </div>
    </div>
  );
}

// ── Day Overview ─────────────────────────────────────────────────
function DayOverview({
  blocks,
  nowHour,
  weeklyProjects,
}: {
  blocks: Block[];
  nowHour: number;
  weeklyProjects: ProjectRow[];
}) {
  const stats = useMemo(() => {
    const agg: Record<Track, number> = { chill: 0, focus: 0, routine: 0 };
    blocks.forEach((b) => {
      if (b.kind !== 'logged') return;
      // Clamp to today's window so overnight/cross-day events don't inflate totals.
      const start = Math.max(0, b.start);
      const end = Math.min(b.end, nowHour);
      if (end <= start) return;
      agg[b.track] += end - start;
    });
    const total = agg.chill + agg.focus + agg.routine;
    return { agg, total };
  }, [blocks, nowHour]);

  return (
    <div className="rhythm-overview">
      <div className="rhythm-ov-row rhythm-ov-balance">
        <div className="rhythm-ov-head">
          <span className="rhythm-kicker">BALANCE · 今日已记录</span>
          <span className="rhythm-ov-total rhythm-mono">
            {stats.total.toFixed(1)}h
          </span>
        </div>
        <div className="rhythm-ov-bar">
          {(['chill', 'focus', 'routine'] as Track[]).map((t) => {
            const pct =
              stats.total > 0 ? (stats.agg[t] / stats.total) * 100 : 33.3;
            return (
              <div
                key={t}
                className={`rhythm-ov-seg rhythm-ov-seg-${t}`}
                style={{ width: `${pct}%` }}
              >
                <span>{TRACKS[t].label}</span>
                <b>{stats.agg[t].toFixed(1)}h</b>
              </div>
            );
          })}
        </div>
      </div>

      <div className="rhythm-ov-row rhythm-ov-next">
        <ProjectsSummary rows={weeklyProjects} />
      </div>
    </div>
  );
}

// ── Planned Feed (已安排 · 流水) ──────────────────────────────────
function PlannedFeed({
  items,
  filter,
  setFilter,
  nowHour,
}: {
  items: PlannedItem[];
  filter: 'all' | Track;
  setFilter: (f: 'all' | Track) => void;
  nowHour: number;
}) {
  const filtered = items.filter((e) => filter === 'all' || e.track === filter);
  const sorted = [...filtered].sort((a, b) => a.start - b.start);
  return (
    <div className="rhythm-todos">
      <div className="rhythm-todos-head">
        <div>
          <span className="rhythm-kicker">已安排 · 流水</span>
          <span className="rhythm-todos-count">{items.length} 条 · 待发生</span>
        </div>
        <div className="rhythm-todos-filters">
          {(
            [
              { k: 'all', lbl: '全部' },
              { k: 'focus', lbl: 'Focus' },
              { k: 'chill', lbl: 'Chill' },
              { k: 'routine', lbl: 'Routine' },
            ] as { k: 'all' | Track; lbl: string }[]
          ).map((f) => (
            <button
              key={f.k}
              className={`rhythm-tf ${filter === f.k ? 'is-on' : ''}`}
              onClick={() => setFilter(f.k)}
            >
              {f.lbl}
            </button>
          ))}
        </div>
      </div>

      <div className="rhythm-feed-list">
        {sorted.length === 0 && (
          <div className="rhythm-ov-proj-empty" style={{ padding: '12px 0' }}>
            今天暂无计划安排。
          </div>
        )}
        {sorted.map((e) => {
          const isPast = e.start < nowHour;
          return (
            <div
              key={e.id}
              className={`rhythm-feed-row rhythm-feed-${e.track}`}
              style={isPast ? { opacity: 0.55 } : undefined}
            >
              <span className="rhythm-feed-time rhythm-mono">
                {fmtTime(e.start)}
              </span>
              <span className={`rhythm-feed-bar rhythm-feed-bar-${e.track}`} />
              <div className="rhythm-feed-body">
                <div className="rhythm-feed-text">
                  {e.project ? `${e.project} · ` : ''}
                  {e.title}
                </div>
                <div className="rhythm-feed-meta">
                  <span className={`rhythm-tag rhythm-tag-${e.track}`}>
                    {TRACKS[e.track].label}
                  </span>
                  {e.note && (
                    <span
                      className="rhythm-feed-proj"
                      style={{ whiteSpace: 'pre-line' }}
                    >
                      {e.note}
                    </span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Root ─────────────────────────────────────────────────────────
export function RhythmView() {
  const [nowDate, setNowDate] = useState(() => new Date());
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [plannedFeed, setPlannedFeed] = useState<PlannedItem[]>([]);
  const [weeklyProjects, setWeeklyProjects] = useState<ProjectRow[]>([]);
  const [filter, setFilter] = useState<'all' | Track>('all');
  const [activeId, setActiveId] = useState<string | null>(null);

  // Live clock: tick every 60s
  useEffect(() => {
    const t = setInterval(() => setNowDate(new Date()), 60 * 1000);
    return () => clearInterval(t);
  }, []);

  const dateStr = fmtDateStr(nowDate);
  const dayBase = useMemo(() => new Date(`${dateStr}T00:00:00`), [dateStr]);
  const nowHour = toDayHours(nowDate, dayBase);

  // Fetch today's timeline + planned
  useEffect(() => {
    const startIso = `${dateStr}T00:00:00`;
    const endIso = `${dateStr}T23:59:59`;

    fetch(
      `/api/timeline?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`,
    )
      .then((r) => r.json())
      .then((data) => {
        const segs = data.segments || [];
        const planned = data.planned_events || [];
        const base = new Date(`${dateStr}T00:00:00`);

        const loggedBlocks: Block[] = [];
        for (const s of segs) {
          const track = categoryToTrack(s.category);
          if (!track) continue;
          const sd = new Date(s.start_time);
          const ongoing = !s.end_time;
          const ed = ongoing ? new Date() : new Date(s.end_time);
          loggedBlocks.push({
            id: String(s.event_ids?.[0] ?? `s-${sd.getTime()}`),
            track,
            title: s.content,
            start: toDayHours(sd, base),
            end: toDayHours(ed, base),
            note: s.notes || '',
            kind: 'logged',
            project: s.project_name || undefined,
            isOngoing: ongoing,
          });
        }

        const intentBlocks: Block[] = [];
        const plannedItems: PlannedItem[] = [];
        for (const e of planned) {
          const track = categoryToTrack(e.category);
          if (!track) continue;
          const sd = new Date(e.start_time);
          const ed = e.end_time
            ? new Date(e.end_time)
            : new Date(sd.getTime() + 30 * 60 * 1000);
          const start = toDayHours(sd, base);
          const end = toDayHours(ed, base);
          const id = String(e.id);
          intentBlocks.push({
            id,
            track,
            title: e.content,
            start,
            end,
            note: e.notes || '',
            kind: 'intent',
            project: e.project_name || undefined,
          });
          plannedItems.push({
            id,
            start,
            end,
            startIso: e.start_time,
            track,
            title: e.content,
            note: e.notes || '',
            project: e.project_name || undefined,
          });
        }

        setBlocks([...loggedBlocks, ...intentBlocks]);
        setPlannedFeed(plannedItems.sort((a, b) => a.start - b.start));
      })
      .catch((err) => console.error('Failed to load rhythm timeline:', err));
  }, [dateStr]);

  // Fetch weekly Focus project breakdown
  useEffect(() => {
    fetch('/api/projects/heatmap?days=7')
      .then((r) => r.json())
      .then((data) => {
        const result: ProjectRow[] = (data.projects || [])
          .map((p: string) => {
            const daily = data.data?.[p] || {};
            const minutes = Object.values(daily).reduce(
              (s: number, v: unknown) => s + (Number(v) || 0),
              0,
            );
            return { name: p, hours: minutes / 60 };
          })
          .filter((r: ProjectRow) => r.hours > 0)
          .sort((a: ProjectRow, b: ProjectRow) => b.hours - a.hours);
        setWeeklyProjects(result);
      })
      .catch((err) =>
        console.error('Failed to load weekly projects:', err),
      );
  }, [dateStr]);

  // Current ongoing block (prefer isOngoing === true, else a logged block that straddles now)
  const currentBlock = useMemo(() => {
    const ongoings = blocks.filter((b) => b.kind === 'logged' && b.isOngoing);
    if (ongoings.length > 0) {
      return [...ongoings].sort((a, b) => b.start - a.start)[0];
    }
    return (
      blocks.find(
        (b) => b.kind === 'logged' && nowHour >= b.start && nowHour < b.end,
      ) || null
    );
  }, [blocks, nowHour]);

  return (
    <div
      className="rhythm-app"
      data-palette="paper"
      data-density="cozy"
      data-now-style="detailed"
    >
      <div className="rhythm-brand">
        <span className="rhythm-brand-mark">LIFE TRACKER · 今日概览</span>
        <div className="rhythm-brand-title">
          {nowDate.getMonth() + 1} 月 {nowDate.getDate()} 日
        </div>
        <div className="rhythm-brand-sub">AI 自动记录 · Focus / Routine / Chill</div>
      </div>

      <EnergyBar nowHour={nowHour} />

      <TimelineRail
        blocks={blocks}
        nowHour={nowHour}
        activeId={activeId}
        onPick={(id) => setActiveId(id === activeId ? null : id)}
        railDate={nowDate}
      />

      <div className="rhythm-main">
        <NowCard block={currentBlock} nowHour={nowHour} />
        <DayOverview
          blocks={blocks}
          nowHour={nowHour}
          weeklyProjects={weeklyProjects}
        />
      </div>

      <PlannedFeed
        items={plannedFeed}
        filter={filter}
        setFilter={setFilter}
        nowHour={nowHour}
      />
    </div>
  );
}
