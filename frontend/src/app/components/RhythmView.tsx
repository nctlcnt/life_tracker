import { useEffect, useMemo, useRef, useState } from 'react';
import { TimelineEvent } from './MultiLaneTimeline';
import './rhythm.css';

// ── Types ─────────────────────────────────────────────────────────
type Track = 'chill' | 'focus' | 'routine';

interface Block {
  id: string;
  track: Track;
  title: string;
  start: number; // 小时浮点，相对当天 00:00
  end: number;
  note: string | null;
  project: string | null;
}

const TRACKS: Record<Track, { label: string }> = {
  chill: { label: 'Chill' },
  focus: { label: 'Focus' },
  routine: { label: 'Routine' },
};

// Morandi 项目色板（与 theme.css 的 chart 色一致），按出现顺序分配
const PROJECT_TONES = ['#94A3B5', '#A8B9A0', '#D4AFA0', '#B5A6BC', '#CFB897'];

const trackOf = (cat: string): Track =>
  cat === 'Focus' ? 'focus' : cat === 'Chill' ? 'chill' : 'routine';

// ── Helpers ───────────────────────────────────────────────────────
const fmtTime = (h: number) => {
  const total = Math.round(h * 60);
  const hh = Math.floor(total / 60);
  const mm = total % 60;
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
};
const fmtDur = (mins: number) => {
  const m = Math.max(0, Math.round(mins));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest === 0 ? `${h}h` : `${h}h ${rest}m`;
};

const WEEKDAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];

// 当前事件判定窗口：最后一段结束时间距现在 5 分钟内视为仍在进行
const NOW_WINDOW_H = 5 / 60;

// ── Day strip：顶部 24h 横条 ──────────────────────────────────────
function DayStrip({ blocks, nowHour }: { blocks: Block[]; nowHour: number | null }) {
  return (
    <div className="rhythm-strip-wrap">
      <div className="rhythm-strip">
        {blocks.map(b => (
          <span
            key={b.id}
            className={`rhythm-strip-seg rhythm-strip-${b.track}`}
            style={{
              left: `${(b.start / 24) * 100}%`,
              width: `${Math.max(0.4, ((b.end - b.start) / 24) * 100)}%`,
            }}
          />
        ))}
        {nowHour != null && (
          <span className="rhythm-strip-now" style={{ left: `${(nowHour / 24) * 100}%` }} />
        )}
      </div>
      <div className="rhythm-strip-axis rhythm-mono">
        {[0, 6, 12, 18, 24].map(h => (
          <span key={h} style={{ left: `${(h / 24) * 100}%` }}>
            {String(h % 24).padStart(2, '0')}
          </span>
        ))}
      </div>
    </div>
  );
}

// ── 顶栏：今日概览 ────────────────────────────────────────────────
function DayPulse({ blocks, nowHour }: { blocks: Block[]; nowHour: number | null }) {
  const focusH = blocks
    .filter(b => b.track === 'focus')
    .reduce((s, b) => s + (b.end - b.start), 0);
  const totalH = blocks.reduce((s, b) => s + (b.end - b.start), 0);

  return (
    <div className="rhythm-energy">
      <div className="rhythm-energy-head">
        <div className="rhythm-energy-title">
          <span className="rhythm-kicker">FOCUS · 今日</span>
          <span className="rhythm-big-num">
            {focusH.toFixed(1)}
            <span className="rhythm-big-num-unit">h</span>
          </span>
          <span className="rhythm-energy-sub">
            共 <b>{blocks.length}</b> 段记录 · 总计 <b>{fmtDur(totalH * 60)}</b>
          </span>
        </div>
        <div className="rhythm-energy-legend">
          {(['focus', 'routine', 'chill'] as Track[]).map(t => (
            <span key={t}>
              <i className={`rhythm-lg rhythm-lg-${t}`} />
              {TRACKS[t].label}{' '}
              {fmtDur(
                blocks
                  .filter(b => b.track === t)
                  .reduce((s, b) => s + (b.end - b.start), 0) * 60
              )}
            </span>
          ))}
        </div>
      </div>
      <DayStrip blocks={blocks} nowHour={nowHour} />
    </div>
  );
}

// 同一泳道内时间重叠的块，按日历的方式分到并列子列，避免互相盖住
function layoutOverlaps(blocks: Block[]): Map<string, { sub: number; subCount: number }> {
  const out = new Map<string, { sub: number; subCount: number }>();
  const sorted = [...blocks].sort((a, b) => a.start - b.start);
  let cluster: Block[] = [];
  let clusterEnd = -Infinity;
  const subEnds: number[] = [];

  const flush = () => {
    const count = subEnds.length;
    cluster.forEach(b => {
      const cur = out.get(b.id)!;
      out.set(b.id, { ...cur, subCount: count });
    });
    cluster = [];
    subEnds.length = 0;
  };

  for (const b of sorted) {
    if (b.start >= clusterEnd && cluster.length > 0) flush();
    let sub = subEnds.findIndex(end => end <= b.start);
    if (sub === -1) {
      sub = subEnds.length;
      subEnds.push(b.end);
    } else {
      subEnds[sub] = b.end;
    }
    out.set(b.id, { sub, subCount: 1 });
    cluster.push(b);
    clusterEnd = Math.max(clusterEnd, b.end);
  }
  if (cluster.length > 0) flush();
  return out;
}

// ── Timeline Rail ─────────────────────────────────────────────────
function TimelineRail({
  blocks,
  nowHour,
  dateLabel,
  activeId,
  onPick,
  startH,
  endH,
}: {
  blocks: Block[];
  nowHour: number | null;
  dateLabel: string;
  activeId: string | null;
  onPick: (id: string) => void;
  startH: number;
  endH: number;
}) {
  const hours = endH - startH;
  const trackOrder: Track[] = ['chill', 'focus', 'routine'];
  const railRef = useRef<HTMLDivElement>(null);

  // 仅初次渲染时滚到当前时间附近，避免每分钟把用户拽走
  useEffect(() => {
    if (!railRef.current || nowHour == null) return;
    const el = railRef.current;
    const y = ((nowHour - startH) / hours) * el.scrollHeight - el.clientHeight / 2;
    el.scrollTo({ top: Math.max(0, y) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="rhythm-rail" ref={railRef}>
      <div className="rhythm-rail-head">
        <div className="rhythm-rail-head-row">
          <span className="rhythm-rail-head-label">Timeline</span>
          <span className="rhythm-rail-head-date">{dateLabel}</span>
        </div>
        <div className="rhythm-rail-tracks">
          {trackOrder.map(t => (
            <div key={t} className={`rhythm-rt rhythm-rt-${t}`}>
              <i />
              <span>{TRACKS[t].label}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="rhythm-rail-body" style={{ ['--rhythm-hours' as string]: hours }}>
        <div className="rhythm-hours">
          {Array.from({ length: hours + 1 }).map((_, i) => {
            const h = startH + i;
            return (
              <div key={h} className="rhythm-hour" style={{ top: `calc(${i} * var(--rhythm-hour-h))` }}>
                <span className="rhythm-hour-lbl">{String(h % 24).padStart(2, '0')}</span>
                <span className="rhythm-hour-line" />
              </div>
            );
          })}
        </div>

        <div className="rhythm-cols">
          {trackOrder.map(t => (
            <div key={t} className={`rhythm-col rhythm-col-${t}`} />
          ))}
        </div>

        <div className="rhythm-blocks">
          {trackOrder.flatMap(t => {
            const trackBlocks = blocks.filter(b => b.track === t);
            const overlaps = layoutOverlaps(trackBlocks);
            return trackBlocks.map(b => ({ b, lay: overlaps.get(b.id)! }));
          }).map(({ b, lay }) => {
            const colIdx = trackOrder.indexOf(b.track);
            const top = ((b.start - startH) / hours) * 100;
            const height = Math.max(((b.end - b.start) / hours) * 100, 0.6);
            const isNow = nowHour != null && nowHour >= b.start && nowHour < b.end + NOW_WINDOW_H;
            return (
              <button
                key={b.id}
                onClick={() => onPick(b.id)}
                className={[
                  'rhythm-blk',
                  `rhythm-blk-${b.track}`,
                  isNow ? 'is-now' : '',
                  activeId === b.id ? 'is-active' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                style={{
                  top: `${top}%`,
                  height: `${height}%`,
                  left: `calc(${colIdx} * (100%/3) + ${lay.sub} * (100%/3) / ${lay.subCount})`,
                  width: `calc(100%/3 / ${lay.subCount})`,
                }}
              >
                <div className="rhythm-blk-time">{fmtTime(b.start)}</div>
                <div className="rhythm-blk-title">{b.title}</div>
                <div className="rhythm-blk-meta">
                  <span>{b.project ?? ''}</span>
                  <span className="rhythm-blk-dur">{fmtDur((b.end - b.start) * 60)}</span>
                </div>
                {isNow && <span className="rhythm-blk-pulse" />}
              </button>
            );
          })}
        </div>

        {nowHour != null && nowHour >= startH && nowHour <= endH && (
          <div className="rhythm-nowline" style={{ top: `${((nowHour - startH) / hours) * 100}%` }}>
            <span className="rhythm-nowline-dot" />
            <span className="rhythm-nowline-lbl">{fmtTime(nowHour)}</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Now Card ──────────────────────────────────────────────────────
function NowCard({
  block,
  nowHour,
  recent,
  isToday,
}: {
  block: Block | null;
  nowHour: number | null;
  recent: Block[];
  isToday: boolean;
}) {
  if (!block) {
    return (
      <div className="rhythm-now-card rhythm-now-card--empty">
        <span className="rhythm-kicker">{isToday ? '正在记录 · 当前' : '历史回看'}</span>
        <h2>{isToday ? '空档 · 无正在进行的记录' : '这一天已经过去'}</h2>
        <p>
          {isToday
            ? '下一条记录出现后会自动填入这里。'
            : '下方是当天的节奏与流水。'}
        </p>
      </div>
    );
  }
  const elapsed = nowHour != null ? Math.max(0, Math.round((nowHour - block.start) * 60)) : 0;
  return (
    <div className={`rhythm-now-card rhythm-now-card--${block.track}`}>
      <div className="rhythm-now-card-head">
        <span className="rhythm-kicker">
          正在记录 · {TRACKS[block.track].label}
          {block.project ? ` · ${block.project}` : ''}
        </span>
        <span className="rhythm-now-card-time rhythm-mono">
          自 {fmtTime(block.start)} · 已 {fmtDur(elapsed)}
        </span>
      </div>
      <h2 className="rhythm-now-card-title">{block.title}</h2>
      {block.note && <p className="rhythm-now-card-note">“{block.note}”</p>}
      <div className="rhythm-now-entries">
        <span className="rhythm-kicker">此前的记录</span>
        {recent.length === 0 && <div className="rhythm-ne-empty">今天的第一条记录。</div>}
        {recent.map(b => (
          <div key={b.id} className="rhythm-ne-row">
            <span className="rhythm-ne-time rhythm-mono">{fmtTime(b.start)}</span>
            <span className="rhythm-ne-dot" />
            <span className="rhythm-ne-text">{b.title}</span>
          </div>
        ))}
      </div>
      <div className="rhythm-now-indicator">
        <span className="rhythm-ni-pulse" />
        <span className="rhythm-ni-label rhythm-mono">LIVE · 由 AI 自动写入</span>
      </div>
    </div>
  );
}

// ── Projects Summary：Focus 时长按项目分解 ────────────────────────
function ProjectsSummary({ blocks }: { blocks: Block[] }) {
  const rows = useMemo(() => {
    const agg: Record<string, number> = {};
    blocks.forEach(b => {
      if (b.track !== 'focus' || !b.project) return;
      agg[b.project] = (agg[b.project] ?? 0) + (b.end - b.start);
    });
    return Object.entries(agg)
      .map(([name, hours], i) => ({ name, hours, tone: PROJECT_TONES[i % PROJECT_TONES.length] }))
      .sort((a, b) => b.hours - a.hours);
  }, [blocks]);

  const total = rows.reduce((s, r) => s + r.hours, 0);
  const max = Math.max(1e-6, ...rows.map(r => r.hours));

  return (
    <div className="rhythm-ov-projects">
      <div className="rhythm-ov-proj-head">
        <span className="rhythm-kicker">PROJECTS · Focus 分解</span>
        <span className="rhythm-ov-proj-total">{total.toFixed(1)}h</span>
      </div>
      <div className="rhythm-ov-proj-list">
        {rows.map(r => (
          <div key={r.name} className="rhythm-ov-proj-row">
            <div className="rhythm-ov-proj-top">
              <span className="rhythm-ov-proj-dot" style={{ background: r.tone }} />
              <span className="rhythm-ov-proj-name">{r.name}</span>
              <span className="rhythm-ov-proj-hrs rhythm-mono">{fmtDur(r.hours * 60)}</span>
            </div>
            <div className="rhythm-ov-proj-bar" style={{ width: `${(r.hours / max) * 100}%` }}>
              <div className="rhythm-ov-proj-fill" style={{ width: '100%', background: r.tone }} />
            </div>
          </div>
        ))}
        {rows.length === 0 && <div className="rhythm-ov-proj-empty">这一天没有挂项目的 Focus 记录。</div>}
      </div>
    </div>
  );
}

// ── Day Overview ─────────────────────────────────────────────────
function DayOverview({ blocks }: { blocks: Block[] }) {
  const stats = useMemo(() => {
    const agg: Record<Track, number> = { chill: 0, focus: 0, routine: 0 };
    blocks.forEach(b => {
      agg[b.track] += b.end - b.start;
    });
    const total = agg.chill + agg.focus + agg.routine;
    return { agg, total };
  }, [blocks]);

  return (
    <div className="rhythm-overview">
      <div className="rhythm-ov-row rhythm-ov-balance">
        <div className="rhythm-ov-head">
          <span className="rhythm-kicker">BALANCE · 当日已记录</span>
          <span className="rhythm-ov-total rhythm-mono">{stats.total.toFixed(1)}h</span>
        </div>
        <div className="rhythm-ov-bar">
          {(['chill', 'focus', 'routine'] as Track[])
            .filter(t => stats.total === 0 || stats.agg[t] > 0)
            .map(t => {
              const pct = stats.total > 0 ? (stats.agg[t] / stats.total) * 100 : 33.3;
              return (
                <div key={t} className={`rhythm-ov-seg rhythm-ov-seg-${t}`} style={{ width: `${pct}%` }}>
                  <span>{TRACKS[t].label}</span>
                  <b>{stats.agg[t].toFixed(1)}h</b>
                </div>
              );
            })}
        </div>
      </div>

      <div className="rhythm-ov-row rhythm-ov-next">
        <ProjectsSummary blocks={blocks} />
      </div>
    </div>
  );
}

// ── Entries Feed：当日事件流水 ────────────────────────────────────
function EntriesFeed({
  blocks,
  filter,
  setFilter,
}: {
  blocks: Block[];
  filter: 'all' | Track;
  setFilter: (f: 'all' | Track) => void;
}) {
  const filtered = blocks.filter(b => filter === 'all' || b.track === filter);
  const sorted = [...filtered].sort((a, b) => b.start - a.start);
  return (
    <div className="rhythm-todos">
      <div className="rhythm-todos-head">
        <div>
          <span className="rhythm-kicker">流水 · 当日</span>
          <span className="rhythm-todos-count">{blocks.length} 条</span>
        </div>
        <div className="rhythm-todos-filters">
          {([
            { k: 'all', lbl: '全部' },
            { k: 'focus', lbl: 'Focus' },
            { k: 'chill', lbl: 'Chill' },
            { k: 'routine', lbl: 'Routine' },
          ] as { k: 'all' | Track; lbl: string }[]).map(f => (
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
        {sorted.map(b => (
          <div key={b.id} className={`rhythm-feed-row rhythm-feed-${b.track}`}>
            <span className="rhythm-feed-time rhythm-mono">{fmtTime(b.start)}</span>
            <span className={`rhythm-feed-bar rhythm-feed-bar-${b.track}`} />
            <div className="rhythm-feed-body">
              <div className="rhythm-feed-text">{b.title}</div>
              <div className="rhythm-feed-meta">
                <span className={`rhythm-tag rhythm-tag-${b.track}`}>{TRACKS[b.track].label}</span>
                {b.project && <span className="rhythm-feed-proj">{b.project}</span>}
                <span>
                  {fmtTime(b.start)}–{fmtTime(b.end)} · {fmtDur((b.end - b.start) * 60)}
                </span>
              </div>
              {b.note && <div className="rhythm-feed-note">{b.note}</div>}
            </div>
          </div>
        ))}
        {sorted.length === 0 && <div className="rhythm-ne-empty">没有记录。</div>}
      </div>
    </div>
  );
}

// ── Root ─────────────────────────────────────────────────────────
export function RhythmView({ events, date }: { events: TimelineEvent[]; date: string }) {
  const [filter, setFilter] = useState<'all' | Track>('all');
  const [activeId, setActiveId] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(t);
  }, []);

  const dayBase = useMemo(() => new Date(`${date}T00:00:00`), [date]);
  const isToday =
    now.getFullYear() === dayBase.getFullYear() &&
    now.getMonth() === dayBase.getMonth() &&
    now.getDate() === dayBase.getDate();
  const nowHour = isToday ? (now.getTime() - dayBase.getTime()) / 3_600_000 : null;

  const blocks = useMemo<Block[]>(
    () =>
      [...events]
        .sort((a, b) => a.startDate.getTime() - b.startDate.getTime())
        .map(ev => ({
          id: ev.id,
          track: trackOf(ev.category),
          title: ev.content,
          start: (ev.startDate.getTime() - dayBase.getTime()) / 3_600_000,
          end: (ev.endDate.getTime() - dayBase.getTime()) / 3_600_000,
          note: ev.notes ?? null,
          project: ev.project_name ?? null,
        })),
    [events, dayBase]
  );

  // 时间轴范围：默认 07–24，事件更早则向上扩
  const startH = Math.max(
    0,
    Math.min(7, ...blocks.map(b => Math.floor(b.start)))
  );
  const endH = 24;

  // 当前进行中的块：今天的最后一段，且结束时间贴近现在
  const currentBlock = useMemo(() => {
    if (nowHour == null || blocks.length === 0) return null;
    const last = blocks[blocks.length - 1];
    return last.end >= nowHour - NOW_WINDOW_H ? last : null;
  }, [blocks, nowHour]);
  const recent = useMemo(
    () => blocks.filter(b => b !== currentBlock).slice(-3).reverse(),
    [blocks, currentBlock]
  );

  const dateLabel = `${WEEKDAYS[dayBase.getDay()]} · ${String(dayBase.getMonth() + 1).padStart(2, '0')}/${String(dayBase.getDate()).padStart(2, '0')}`;

  return (
    <div className="rhythm-app">
      <div className="rhythm-brand">
        <span className="rhythm-brand-mark">RHYTHM · LIFE LOG</span>
        <div className="rhythm-brand-title">今日节奏</div>
        <div className="rhythm-brand-sub">AI 自动记录 · Chill / Focus / Routine</div>
      </div>

      <DayPulse blocks={blocks} nowHour={nowHour} />

      <TimelineRail
        blocks={blocks}
        nowHour={nowHour}
        dateLabel={dateLabel}
        activeId={activeId}
        onPick={id => setActiveId(id === activeId ? null : id)}
        startH={startH}
        endH={endH}
      />

      <div className="rhythm-main">
        <NowCard block={currentBlock} nowHour={nowHour} recent={recent} isToday={isToday} />
        <DayOverview blocks={blocks} />
      </div>

      <EntriesFeed blocks={blocks} filter={filter} setFilter={setFilter} />
    </div>
  );
}
