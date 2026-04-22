import { useEffect, useMemo, useRef, useState } from 'react';
import './rhythm.css';

// ── Types ─────────────────────────────────────────────────────────
type Track = 'chill' | 'focus' | 'routine';
type BlockKind = 'logged' | 'intent';
type EntrySource = 'auto' | 'ai' | 'manual';
type ProjectId = 'atlas' | 'beacon' | 'meridian' | 'writing';

interface Block {
  id: string;
  track: Track;
  title: string;
  start: number;
  end: number;
  cost: number;
  note: string;
  kind: BlockKind;
  project?: ProjectId;
}

interface Entry {
  id: string;
  at: number;
  track: Track;
  text: string;
  source: EntrySource;
  project?: ProjectId;
}

// ── Seed data ─────────────────────────────────────────────────────
const TRACKS: Record<Track, { label: string }> = {
  chill: { label: 'Chill' },
  focus: { label: 'Focus' },
  routine: { label: 'Routine' },
};

const PROJECTS: Record<ProjectId, { label: string; tone: string }> = {
  atlas: { label: 'Atlas 报告', tone: 'oklch(55% 0.12 40)' },
  beacon: { label: 'Beacon', tone: 'oklch(50% 0.10 240)' },
  meridian: { label: 'Meridian', tone: 'oklch(52% 0.12 160)' },
  writing: { label: '个人写作', tone: 'oklch(45% 0.06 310)' },
};

const SEED_BLOCKS: Block[] = [
  { id: 'b1', track: 'routine', title: '起床 · 洗漱 · 早餐', start: 7.0, end: 8.0, cost: 4, note: '慢启动', kind: 'logged' },
  { id: 'b2', track: 'chill', title: '晨读 · 咖啡', start: 8.0, end: 9.0, cost: 2, note: '《失控》p.120', kind: 'logged' },
  { id: 'b3', track: 'focus', title: '深度工作 · 报告起草', start: 9.0, end: 11.5, cost: 14, note: '无通知', project: 'atlas', kind: 'logged' },
  { id: 'b4', track: 'routine', title: '午餐 · 散步', start: 11.5, end: 12.5, cost: 3, note: '', kind: 'logged' },
  { id: 'b5', track: 'focus', title: '会议 · 产品评审', start: 13.0, end: 14.5, cost: 9, note: '带 2 页稿', project: 'beacon', kind: 'logged' },
  { id: 'b6', track: 'chill', title: '咖啡 · 整理桌面', start: 14.5, end: 15.0, cost: 1, note: '', kind: 'logged' },
  { id: 'b7', track: 'focus', title: '代码 · 修 bug #882', start: 15.0, end: 17.0, cost: 10, note: '', project: 'meridian', kind: 'logged' },
  { id: 'b8', track: 'routine', title: '健身 · 拉伸', start: 17.5, end: 18.5, cost: 6, note: '推日', kind: 'intent' },
  { id: 'b9', track: 'chill', title: '晚餐 · 看剧', start: 18.5, end: 20.0, cost: 0, note: '', kind: 'intent' },
  { id: 'b10', track: 'focus', title: '写作 · 周记', start: 20.5, end: 21.5, cost: 5, note: '', project: 'writing', kind: 'intent' },
  { id: 'b11', track: 'chill', title: '阅读 · 睡前', start: 22.0, end: 23.0, cost: 0, note: '', kind: 'intent' },
];

const SEED_ENTRIES: Entry[] = [
  { id: 'e1', at: 7.05, track: 'routine', text: '起床，有点赖床 · 泡了咖啡', source: 'auto' },
  { id: 'e2', at: 8.02, track: 'chill', text: '翻了几页《失控》p.120 起', source: 'ai' },
  { id: 'e3', at: 9.1, track: 'focus', text: '进入 Atlas 报告草稿 · 无通知', source: 'auto', project: 'atlas' },
  { id: 'e4', at: 10.4, track: 'focus', text: '卡在第 3 节结构 · 换了思路', source: 'manual', project: 'atlas' },
  { id: 'e5', at: 11.35, track: 'routine', text: '午饭 + 园区散步 12 分钟', source: 'ai' },
  { id: 'e6', at: 13.0, track: 'focus', text: 'Beacon 评审 · 带两页稿', source: 'auto', project: 'beacon' },
  { id: 'e7', at: 14.28, track: 'chill', text: '桌面清理 + 一杯手冲', source: 'ai' },
  { id: 'e8', at: 15.02, track: 'focus', text: '修 bug #882 · 先复现', source: 'auto', project: 'meridian' },
  { id: 'e9', at: 16.12, track: 'focus', text: '找到原因：race condition in scheduler', source: 'manual', project: 'meridian' },
];

const TWEAK_DEFAULTS = {
  capacity: 80,
  now: 10.15,
  palette: 'paper' as 'paper' | 'slate' | 'olive',
  density: 'cozy' as 'cozy' | 'comfy' | 'roomy',
  nowStyle: 'detailed' as 'detailed' | 'minimal',
};

// ── Helpers ───────────────────────────────────────────────────────
const fmtTime = (h: number) => {
  const hh = Math.floor(h);
  const mm = Math.round((h - hh) * 60);
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
};
const fmtDur = (mins: number) => {
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
};

// ── Energy Bar ────────────────────────────────────────────────────
function EnergyBar({
  capacity,
  spent,
  planned,
  nowHour,
  blocks,
  onScrub,
}: {
  capacity: number;
  spent: number;
  planned: number;
  nowHour: number;
  blocks: Block[];
  onScrub: (h: number) => void;
}) {
  const remaining = Math.max(0, capacity - spent);
  const afterPlanned = Math.max(0, capacity - spent - planned);
  const pctSpent = (spent / capacity) * 100;
  const pctPlanned = (planned / capacity) * 100;

  const startH = 7;
  const endH = 24;
  const steps: { h: number; e: number }[] = [];
  let e = capacity;
  for (let h = startH; h <= endH; h += 0.25) {
    const active = blocks.find((b) => h >= b.start && h < b.end);
    if (active) {
      e = Math.max(
        0,
        e - (active.cost * 0.25) / Math.max(0.25, active.end - active.start),
      );
    }
    if (active && active.track === 'chill') e = Math.min(capacity, e + 0.25);
    steps.push({ h, e });
  }
  const maxE = capacity;
  const pathD = steps
    .map((s, i) => {
      const x = ((s.h - startH) / (endH - startH)) * 100;
      const y = 100 - (s.e / maxE) * 100;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');

  const nowX = ((nowHour - startH) / (endH - startH)) * 100;

  return (
    <div className="rhythm-energy">
      <div className="rhythm-energy-head">
        <div className="rhythm-energy-title">
          <span className="rhythm-kicker">ENERGY · 今日已消耗</span>
          <span className="rhythm-big-num">
            {Math.round(spent)}
            <span className="rhythm-big-num-unit">/{capacity}</span>
          </span>
          <span className="rhythm-energy-sub">
            剩余 <b>{Math.round(remaining)}</b> · 估算意向还将消耗 {Math.round(planned)}
          </span>
        </div>
        <div className="rhythm-energy-legend">
          <span>
            <i className="rhythm-lg rhythm-lg-spent" />已用 {Math.round(spent)}
          </span>
          <span>
            <i className="rhythm-lg rhythm-lg-planned" />意向 {Math.round(planned)}
          </span>
          <span>
            <i className="rhythm-lg rhythm-lg-free" />余 {Math.round(afterPlanned)}
          </span>
        </div>
      </div>

      <div className="rhythm-energy-bar">
        <div className="rhythm-eb-seg rhythm-eb-spent" style={{ width: `${pctSpent}%` }} />
        <div className="rhythm-eb-seg rhythm-eb-planned" style={{ width: `${pctPlanned}%` }} />
        <div className="rhythm-eb-ticks">
          {Array.from({ length: 11 }).map((_, i) => (
            <span key={i} style={{ left: `${i * 10}%` }} />
          ))}
        </div>
      </div>

      <div className="rhythm-energy-forecast">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="rhythm-fc-svg">
          <defs>
            <linearGradient id="rhythm-fc-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="var(--rhythm-ink)" stopOpacity="0.14" />
              <stop offset="1" stopColor="var(--rhythm-ink)" stopOpacity="0" />
            </linearGradient>
            <clipPath id="rhythm-fc-clip-past">
              <rect x="0" y="0" width={nowX} height="100" />
            </clipPath>
            <clipPath id="rhythm-fc-clip-future">
              <rect x={nowX} y="0" width={100 - nowX} height="100" />
            </clipPath>
          </defs>
          <path d={`${pathD} L100,100 L0,100 Z`} fill="url(#rhythm-fc-fill)" clipPath="url(#rhythm-fc-clip-past)" />
          <path
            d={pathD}
            fill="none"
            stroke="var(--rhythm-ink)"
            strokeWidth="0.7"
            vectorEffect="non-scaling-stroke"
            clipPath="url(#rhythm-fc-clip-past)"
          />
          <path
            d={pathD}
            fill="none"
            stroke="var(--rhythm-ink-3)"
            strokeWidth="0.5"
            strokeDasharray="1.5 1.5"
            vectorEffect="non-scaling-stroke"
            clipPath="url(#rhythm-fc-clip-future)"
          />
          <line
            x1={nowX}
            y1="0"
            x2={nowX}
            y2="100"
            stroke="var(--rhythm-accent)"
            strokeWidth="0.8"
            vectorEffect="non-scaling-stroke"
          />
        </svg>
        <div className="rhythm-fc-axis">
          {[7, 10, 13, 16, 19, 22].map((h) => (
            <span key={h} style={{ left: `${((h - startH) / (endH - startH)) * 100}%` }}>
              {h}:00
            </span>
          ))}
        </div>
        <input
          className="rhythm-fc-scrub"
          type="range"
          min={startH * 100}
          max={endH * 100}
          step={5}
          value={Math.round(nowHour * 100)}
          onChange={(e) => onScrub(Number(e.target.value) / 100)}
          aria-label="拖动时间"
        />
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
  startH = 7,
  endH = 24,
}: {
  blocks: Block[];
  nowHour: number;
  activeId: string | null;
  onPick: (id: string) => void;
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

  return (
    <div className="rhythm-rail" ref={railRef}>
      <div className="rhythm-rail-head">
        <div className="rhythm-rail-head-row">
          <span className="rhythm-rail-head-label">Timeline</span>
          <span className="rhythm-rail-head-date">THU · 04/21</span>
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

      <div className="rhythm-rail-body" style={{ ['--rhythm-hours' as string]: hours }}>
        <div className="rhythm-hours">
          {Array.from({ length: hours + 1 }).map((_, i) => {
            const h = startH + i;
            return (
              <div key={h} className="rhythm-hour" style={{ top: `calc(${i} * var(--rhythm-hour-h))` }}>
                <span className="rhythm-hour-lbl">{String(h).padStart(2, '0')}</span>
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
          {blocks.map((b) => {
            const colIdx = trackOrder.indexOf(b.track);
            const top = ((b.start - startH) / hours) * 100;
            const height = ((b.end - b.start) / hours) * 100;
            const isIntent = b.kind === 'intent';
            const isNow = nowHour >= b.start && nowHour < b.end;
            return (
              <button
                key={b.id}
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
              >
                <div className="rhythm-blk-time">
                  {fmtTime(b.start)}
                  {isIntent && <span className="rhythm-blk-intent-tag"> · 意向</span>}
                </div>
                <div className="rhythm-blk-title">{b.title}</div>
                <div className="rhythm-blk-meta">
                  <span className="rhythm-blk-cost">{b.cost}⌁</span>
                  <span className="rhythm-blk-dur">{fmtDur(Math.round((b.end - b.start) * 60))}</span>
                </div>
                {isNow && <span className="rhythm-blk-pulse" />}
              </button>
            );
          })}
        </div>

        <div className="rhythm-nowline" style={{ top: `${((nowHour - startH) / hours) * 100}%` }}>
          <span className="rhythm-nowline-dot" />
          <span className="rhythm-nowline-lbl">{fmtTime(nowHour)}</span>
        </div>
      </div>
    </div>
  );
}

// ── Now Card ──────────────────────────────────────────────────────
function NowCard({ block, nowHour, entries }: { block: Block | null; nowHour: number; entries: Entry[] }) {
  const blockEntries = block ? entries.filter((e) => e.at >= block.start && e.at <= nowHour).slice(-3) : [];
  if (!block) {
    return (
      <div className="rhythm-now-card rhythm-now-card--empty">
        <span className="rhythm-kicker">正在记录 · 当前</span>
        <h2>空档 · 无正在进行的记录</h2>
        <p>AI 没有检测到正在进行的活动。下一条记录出现后会自动填入这里。</p>
      </div>
    );
  }
  const elapsed = Math.max(0, Math.round((nowHour - block.start) * 60));
  return (
    <div className={`rhythm-now-card rhythm-now-card--${block.track}`}>
      <div className="rhythm-now-card-head">
        <span className="rhythm-kicker">
          正在记录 · {TRACKS[block.track].label}
          {block.project ? ` · ${PROJECTS[block.project]?.label || block.project}` : ''}
        </span>
        <span className="rhythm-now-card-time rhythm-mono">
          自 {fmtTime(block.start)} · 已 {fmtDur(elapsed)}
        </span>
      </div>
      <h2 className="rhythm-now-card-title">{block.title}</h2>
      {block.note && <p className="rhythm-now-card-note">“{block.note}”</p>}
      <div className="rhythm-now-entries">
        <span className="rhythm-kicker">最近自动捕获</span>
        {blockEntries.length === 0 && <div className="rhythm-ne-empty">等待下一条自动记录…</div>}
        {blockEntries.map((e) => (
          <div key={e.id} className="rhythm-ne-row">
            <span className="rhythm-ne-time rhythm-mono">{fmtTime(e.at)}</span>
            <span className="rhythm-ne-dot" />
            <span className="rhythm-ne-text">{e.text}</span>
            <span className={`rhythm-ne-src rhythm-ne-src-${e.source}`}>{e.source}</span>
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

// ── Projects Summary ─────────────────────────────────────────────
function ProjectsSummary({ blocks, nowHour }: { blocks: Block[]; nowHour: number }) {
  const rows = useMemo(() => {
    const agg: Record<string, { planned: number; done: number }> = {};
    blocks.forEach((b) => {
      if (b.track !== 'focus' || !b.project) return;
      const dur = b.end - b.start;
      const donePortion = Math.max(0, Math.min(dur, nowHour - b.start));
      if (!agg[b.project]) agg[b.project] = { planned: 0, done: 0 };
      agg[b.project].planned += dur;
      agg[b.project].done += donePortion;
    });
    return Object.entries(agg)
      .map(([id, v]) => ({ id: id as ProjectId, ...PROJECTS[id as ProjectId], ...v }))
      .sort((a, b) => b.planned - a.planned);
  }, [blocks, nowHour]);

  const totalPlanned = rows.reduce((s, r) => s + r.planned, 0);
  const maxPlanned = Math.max(1, ...rows.map((r) => r.planned));

  return (
    <div className="rhythm-ov-projects">
      <div className="rhythm-ov-proj-head">
        <span className="rhythm-kicker">PROJECTS · Focus 分解</span>
        <span className="rhythm-ov-proj-total">{totalPlanned.toFixed(1)}h</span>
      </div>
      <div className="rhythm-ov-proj-list">
        {rows.map((r) => {
          const pct = (r.done / r.planned) * 100;
          const wPct = (r.planned / maxPlanned) * 100;
          return (
            <div key={r.id} className="rhythm-ov-proj-row">
              <div className="rhythm-ov-proj-top">
                <span className="rhythm-ov-proj-dot" style={{ background: r.tone }} />
                <span className="rhythm-ov-proj-name">{r.label}</span>
                <span className="rhythm-ov-proj-hrs rhythm-mono">
                  {r.done.toFixed(1)}/{r.planned.toFixed(1)}h
                </span>
              </div>
              <div className="rhythm-ov-proj-bar" style={{ width: `${wPct}%` }}>
                <div className="rhythm-ov-proj-fill" style={{ width: `${pct}%`, background: r.tone }} />
              </div>
            </div>
          );
        })}
        {rows.length === 0 && <div className="rhythm-ov-proj-empty">今日无 Focus 项目。</div>}
      </div>
    </div>
  );
}

// ── Day Overview ─────────────────────────────────────────────────
function DayOverview({ blocks, nowHour }: { blocks: Block[]; nowHour: number }) {
  const stats = useMemo(() => {
    const agg: Record<Track, number> = { chill: 0, focus: 0, routine: 0 };
    blocks.forEach((b) => {
      if (b.kind === 'intent') return;
      const end = Math.min(b.end, nowHour);
      if (end <= b.start) return;
      agg[b.track] += end - b.start;
    });
    const total = agg.chill + agg.focus + agg.routine;
    return { agg, total };
  }, [blocks, nowHour]);

  return (
    <div className="rhythm-overview">
      <div className="rhythm-ov-row rhythm-ov-balance">
        <div className="rhythm-ov-head">
          <span className="rhythm-kicker">BALANCE · 今日已记录</span>
          <span className="rhythm-ov-total rhythm-mono">{stats.total.toFixed(1)}h</span>
        </div>
        <div className="rhythm-ov-bar">
          {(['chill', 'focus', 'routine'] as Track[]).map((t) => {
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
        <ProjectsSummary blocks={blocks} nowHour={nowHour} />
      </div>
    </div>
  );
}

// ── Entries Feed ─────────────────────────────────────────────────
function EntriesFeed({
  entries,
  filter,
  setFilter,
}: {
  entries: Entry[];
  filter: 'all' | Track;
  setFilter: (f: 'all' | Track) => void;
}) {
  const filtered = entries.filter((e) => filter === 'all' || e.track === filter);
  const sorted = [...filtered].sort((a, b) => b.at - a.at);
  return (
    <div className="rhythm-todos">
      <div className="rhythm-todos-head">
        <div>
          <span className="rhythm-kicker">已安排 · 流水</span>
          <span className="rhythm-todos-count">{entries.length} 条 · 今日</span>
        </div>
        <div className="rhythm-todos-filters">
          {([
            { k: 'all', lbl: '全部' },
            { k: 'focus', lbl: 'Focus' },
            { k: 'chill', lbl: 'Chill' },
            { k: 'routine', lbl: 'Routine' },
          ] as { k: 'all' | Track; lbl: string }[]).map((f) => (
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
        {sorted.map((e) => (
          <div key={e.id} className={`rhythm-feed-row rhythm-feed-${e.track}`}>
            <span className="rhythm-feed-time rhythm-mono">{fmtTime(e.at)}</span>
            <span className={`rhythm-feed-bar rhythm-feed-bar-${e.track}`} />
            <div className="rhythm-feed-body">
              <div className="rhythm-feed-text">{e.text}</div>
              <div className="rhythm-feed-meta">
                <span className={`rhythm-tag rhythm-tag-${e.track}`}>{TRACKS[e.track].label}</span>
                {e.project && <span className="rhythm-feed-proj">{PROJECTS[e.project]?.label}</span>}
                <span className={`rhythm-feed-src rhythm-feed-src-${e.source}`}>
                  {e.source === 'auto' && '◉ 自动'}
                  {e.source === 'ai' && '✦ AI 摘要'}
                  {e.source === 'manual' && '✎ 手动补充'}
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Root ─────────────────────────────────────────────────────────
export function RhythmView() {
  const [tweaks, setTweaks] = useState(TWEAK_DEFAULTS);
  const [blocks] = useState(SEED_BLOCKS);
  const [entries] = useState(SEED_ENTRIES);
  const [filter, setFilter] = useState<'all' | Track>('all');
  const [activeId, setActiveId] = useState<string | null>(null);

  const nowHour = tweaks.now;

  const { spent, planned } = useMemo(() => {
    let s = 0;
    let p = 0;
    blocks.forEach((b) => {
      if (b.kind === 'intent') {
        if (b.start >= nowHour) p += b.cost;
        else {
          const frac = Math.max(0, (nowHour - b.start) / (b.end - b.start));
          s += b.cost * frac;
          p += b.cost * (1 - frac);
        }
      } else {
        if (b.end <= nowHour) s += b.cost;
        else if (b.start >= nowHour) p += b.cost;
        else {
          const frac = (nowHour - b.start) / (b.end - b.start);
          s += b.cost * frac;
          p += b.cost * (1 - frac);
        }
      }
    });
    return { spent: s, planned: p };
  }, [blocks, nowHour]);

  const currentBlock = blocks.find((b) => nowHour >= b.start && nowHour < b.end) || null;

  return (
    <div
      className="rhythm-app"
      data-palette={tweaks.palette}
      data-density={tweaks.density}
      data-now-style={tweaks.nowStyle}
    >
      <div className="rhythm-brand">
        <span className="rhythm-brand-mark">RHYTHM · v0.5 · LOG · MOCK</span>
        <div className="rhythm-brand-title">时间管理系统</div>
        <div className="rhythm-brand-sub">AI 自动记录 · Chill / Focus / Routine</div>
      </div>

      <EnergyBar
        capacity={tweaks.capacity}
        spent={spent}
        planned={planned}
        nowHour={nowHour}
        blocks={blocks}
        onScrub={(h) => setTweaks({ ...tweaks, now: h })}
      />

      <TimelineRail
        blocks={blocks}
        nowHour={nowHour}
        activeId={activeId}
        onPick={(id) => setActiveId(id === activeId ? null : id)}
      />

      <div className="rhythm-main">
        <NowCard block={currentBlock} nowHour={nowHour} entries={entries} />
        <DayOverview blocks={blocks} nowHour={nowHour} />
      </div>

      <EntriesFeed entries={entries} filter={filter} setFilter={setFilter} />
    </div>
  );
}
