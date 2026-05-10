import { useEffect, useState } from 'react';
import './trace.css';

// ── Types ─────────────────────────────────────────────────────

interface PromptParts {
  block1_static: {
    identity: string;
    user_model: string;
    system_mechanics: string;
    communication: string;
    protocols: string;
    tools_section: string;
  };
  block2_projects: string;
  block3_memories: string;
  block4_dynamic: {
    ongoing: string;
    pending_reminders: string;
    deadlines: string;
    weather: string;
  };
}

interface ToolCall {
  name: string;
  input: Record<string, unknown>;
  id?: string;
}

interface ToolResult {
  name: string;
  result: unknown;
  tool_use_id?: string;
}

interface Round {
  n: number;
  raw_output: string;
  think: string;
  visible_text: string;
  tool_calls: ToolCall[];
  tool_results: ToolResult[];
  post_hints_triggered: string[];
  usage?: Record<string, unknown> | null;
  stop_reason?: string | null;
}

interface HistoryMsg {
  role: string;
  content: unknown;
}

interface TraceEntry {
  id: string;
  ts: string;
  trigger: string;
  model: string;
  provider: string;
  prompt_parts: PromptParts | null;
  user_message: string;
  history: HistoryMsg[];
  rounds: Round[];
  final_text: string | null;
  error: string | null;
}

interface DateInfo {
  date: string;
  count: number;
}

// ── Helpers ───────────────────────────────────────────────────

const TRIGGER_ICONS: Record<string, string> = {
  chat: '💬',
  poll: '🔄',
  reminder: '🔔',
  bedtime: '😴',
  morning: '🌅',
  oneshot: '📝',
  scheduled: '📋',
};

function fmtTime(ts: string): string {
  // ts is ISO like 2026-05-09T14:32:05
  const t = ts.split('T')[1] || ts;
  return t.slice(0, 8);
}

function uniqueToolNames(rounds: Round[]): string[] {
  const set = new Set<string>();
  for (const r of rounds) {
    for (const tc of r.tool_calls || []) set.add(tc.name);
  }
  return [...set];
}

function fmtPreview(s: string, max = 80): string {
  const trimmed = (s || '').replace(/\s+/g, ' ').trim();
  if (trimmed.length <= max) return trimmed;
  return trimmed.slice(0, max) + '…';
}

function fmtJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function fmtToolInput(input: Record<string, unknown>): string {
  // Compact one-line form for the call header
  const entries = Object.entries(input || {});
  if (entries.length === 0) return '';
  return entries
    .map(([k, v]) => {
      let s: string;
      if (typeof v === 'string') s = JSON.stringify(v);
      else s = fmtJson(v).replace(/\n\s*/g, ' ');
      if (s.length > 60) s = s.slice(0, 57) + '…';
      return `${k}=${s}`;
    })
    .join(', ');
}

function contentToText(c: unknown): string {
  if (c == null) return '';
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    return c
      .map(b => {
        if (typeof b === 'string') return b;
        if (b && typeof b === 'object') {
          const obj = b as Record<string, unknown>;
          if (typeof obj.text === 'string') return obj.text;
          if (typeof obj.content === 'string') return obj.content;
          return fmtJson(b);
        }
        return String(b);
      })
      .join('\n');
  }
  return fmtJson(c);
}

// ── Components ────────────────────────────────────────────────

function PromptPartsView({ parts }: { parts: PromptParts | null }) {
  if (!parts) return <div className="trace-block-empty">无 prompt（轻量调用）</div>;
  const blocks: Array<{ label: string; text: string }> = [
    { label: 'Block 1 · Identity', text: parts.block1_static.identity },
    { label: 'Block 1 · User Model', text: parts.block1_static.user_model },
    { label: 'Block 1 · System Mechanics', text: parts.block1_static.system_mechanics },
    { label: 'Block 1 · Communication', text: parts.block1_static.communication },
    { label: 'Block 1 · Protocols', text: parts.block1_static.protocols },
    { label: 'Block 1 · Tools Section', text: parts.block1_static.tools_section },
    { label: 'Block 2 · Projects', text: parts.block2_projects },
    { label: 'Block 3 · Memories', text: parts.block3_memories },
    { label: 'Block 4 · Ongoing', text: parts.block4_dynamic.ongoing },
    { label: 'Block 4 · Pending Reminders', text: parts.block4_dynamic.pending_reminders },
    { label: 'Block 4 · Deadlines', text: parts.block4_dynamic.deadlines },
    { label: 'Block 4 · Weather', text: parts.block4_dynamic.weather },
  ];
  return (
    <>
      {blocks.map((b, i) => (
        <div className="trace-block" key={i}>
          <div className="trace-block-head">{b.label}</div>
          {b.text ? (
            <div className="trace-block-text">{b.text}</div>
          ) : (
            <div className="trace-block-empty">（空）</div>
          )}
        </div>
      ))}
    </>
  );
}

function RoundView({ round }: { round: Round }) {
  return (
    <div className="trace-round">
      <div className="trace-round-head">
        <span>Round {round.n}</span>
        {round.stop_reason && <span className="trace-round-stop">stop: {round.stop_reason}</span>}
        {round.usage && (
          <span className="trace-usage">
            {Object.entries(round.usage)
              .filter(([, v]) => typeof v === 'number')
              .map(([k, v]) => `${k}=${v}`)
              .join(' · ')}
          </span>
        )}
      </div>

      {round.think && <div className="trace-think">{round.think}</div>}

      {round.tool_calls.map((tc, i) => {
        const result = round.tool_results.find(
          r => (tc.id && r.tool_use_id === tc.id) || r.name === tc.name
        );
        const hit = round.post_hints_triggered?.includes(tc.name);
        return (
          <div className="trace-tool" key={i}>
            <div className="trace-tool-call">
              {tc.name}({fmtToolInput(tc.input)})
              {hit && <span className="trace-post-hint-tag">post-hint</span>}
            </div>
            {result && (
              <div className="trace-tool-result">→ {fmtJson(result.result)}</div>
            )}
          </div>
        );
      })}

      {round.visible_text && <div className="trace-visible">{round.visible_text}</div>}
    </div>
  );
}

function TraceItem({ entry }: { entry: TraceEntry }) {
  const [expanded, setExpanded] = useState(false);
  const trigger = entry.trigger || 'scheduled';
  const icon = TRIGGER_ICONS[trigger] || '📋';
  const tools = uniqueToolNames(entry.rounds);

  return (
    <div className={`trace-item ${expanded ? 'expanded' : ''} ${entry.error ? 'has-error' : ''}`}>
      <div className="trace-summary" onClick={() => setExpanded(e => !e)}>
        <div className="trace-summary-row">
          <span className="trace-time">{fmtTime(entry.ts)}</span>
          <span className={`trace-trigger ${trigger}`}>{icon} {trigger}</span>
          <span>{entry.provider}/{entry.model}</span>
          <span className="trace-rounds-badge">· {entry.rounds.length} round{entry.rounds.length !== 1 ? 's' : ''}</span>
          {tools.length > 0 && (
            <span className="trace-tools-badge">· {tools.join(', ')}</span>
          )}
          {entry.error && <span className="trace-error-badge">· error</span>}
        </div>
        <div className="trace-preview">
          {trigger === 'chat'
            ? `→ ${fmtPreview(entry.user_message)}`
            : entry.final_text
              ? `← ${fmtPreview(entry.final_text)}`
              : entry.user_message
                ? `[${fmtPreview(entry.user_message, 60)}]`
                : '(silent)'}
        </div>
      </div>

      {expanded && (
        <div className="trace-detail">
          {entry.user_message && (
            <div className="trace-section">
              <div className="trace-section-label">用户消息 / 触发指令</div>
              <div className="trace-section-body">{entry.user_message}</div>
            </div>
          )}

          <div className="trace-section">
            <div className="trace-section-label">AI 响应</div>
            {entry.rounds.length === 0 && (
              <div className="trace-block-empty">（无轮次记录）</div>
            )}
            {entry.rounds.map(r => <RoundView key={r.n} round={r} />)}
          </div>

          {entry.error && (
            <div className="trace-section">
              <div className="trace-section-label">错误</div>
              <div className="trace-error">{entry.error}</div>
            </div>
          )}

          {entry.final_text !== null && (
            <div className="trace-section">
              <div className="trace-section-label">最终发出（拼接所有 visible 轮次）</div>
              <div className="trace-final">{entry.final_text || '(silent — 无消息发出)'}</div>
            </div>
          )}

          <details className="trace-collapsible">
            <summary>Prompt 组成（4 层 block）</summary>
            <div className="trace-collapsible-body">
              <PromptPartsView parts={entry.prompt_parts} />
            </div>
          </details>

          {entry.history.length > 0 && (
            <details className="trace-collapsible">
              <summary>历史消息（{entry.history.length}）</summary>
              <div className="trace-collapsible-body">
                {entry.history.map((m, i) => (
                  <div className="trace-history-msg" key={i}>
                    <span className="trace-history-role">{m.role}</span>
                    <div className="trace-history-content">{contentToText(m.content)}</div>
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

const TRIGGER_OPTIONS = ['', 'chat', 'poll', 'reminder', 'bedtime', 'morning', 'oneshot'];

export function TraceViewer() {
  const [dates, setDates] = useState<DateInfo[]>([]);
  const [date, setDate] = useState<string>('');
  const [trigger, setTrigger] = useState<string>('');
  const [traces, setTraces] = useState<TraceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Load available dates on mount
  useEffect(() => {
    fetch('/api/traces/dates')
      .then(r => r.json())
      .then(data => {
        const ds: DateInfo[] = data.dates || [];
        setDates(ds);
        if (ds.length > 0 && !date) {
          setDate(ds[0].date);
        }
      })
      .catch(e => setErr(String(e)));
  }, []);

  // Reload traces whenever date or trigger changes
  useEffect(() => {
    if (!date) return;
    setLoading(true);
    setErr(null);
    const params = new URLSearchParams({ date, limit: '200' });
    if (trigger) params.set('trigger', trigger);
    fetch(`/api/traces?${params}`)
      .then(r => r.json())
      .then(data => setTraces(data.traces || []))
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [date, trigger]);

  return (
    <div className="trace-panel">
      <div className="trace-header">
        <div>
          <a href="#" className="trace-back">← 回到 Dashboard</a>
          <h1>AI Traces</h1>
        </div>
        <div className="trace-controls">
          <select value={date} onChange={e => setDate(e.target.value)}>
            {dates.length === 0 && <option value="">（尚无 trace）</option>}
            {dates.map(d => (
              <option key={d.date} value={d.date}>
                {d.date} · {d.count}
              </option>
            ))}
          </select>
          <select value={trigger} onChange={e => setTrigger(e.target.value)}>
            {TRIGGER_OPTIONS.map(t => (
              <option key={t} value={t}>{t || '所有触发源'}</option>
            ))}
          </select>
        </div>
      </div>

      {err && <div className="trace-msg err">{err}</div>}
      {loading && <div className="trace-msg">加载中…</div>}
      {!loading && !err && traces.length === 0 && (
        <div className="trace-msg">这一天还没有 trace。</div>
      )}

      <div className="trace-list">
        {traces.map(t => <TraceItem key={t.id} entry={t} />)}
      </div>
    </div>
  );
}
