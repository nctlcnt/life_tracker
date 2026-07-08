import { useEffect, useRef, useState } from 'react';
import './admin.css';

interface Preset {
  name: string;
  provider: string;
  model: string;
  base_url: string;
  use_v1_suffix: boolean;
  note: string;
  api_key_masked: string;
}

interface PresetsResp {
  presets: Preset[];
  active: string | null;
  fallback: string | null;
}

interface TestResult {
  ok: boolean;
  reply?: string;
  error?: string;
  latency_ms: number;
  provider: string;
  model: string;
}

interface PromptSection {
  key: string;
  label: string;
  value: string;
  current_value: string;
  updated_at: string | null;
  empty: boolean;
  // 已内联进 main_template 的旧散文 section：API 保留读写，UI 不再展示
  hidden?: boolean;
}

interface PromptsResp {
  sections: PromptSection[];
}

interface CheckIn {
  id: number;
  name: string;
  label: string;
  enabled: boolean;
  schedule_type: 'window' | 'after_ai_call';
  time_start: string | null;
  time_end: string | null;
  days_of_week: number[] | null;
  interval_min_minutes: number | null;
  interval_max_minutes: number | null;
  prompt_template: string;
  instructions?: string;
  context_config?: Record<string, boolean>;
  tool_profile: 'poll' | 'reminder_safe' | 'none';
  allow_silent: boolean;
  last_scheduled_for: string | null;
  last_fired_at: string | null;
  built_in: boolean;
}

interface CheckInsResp {
  check_ins: CheckIn[];
  settings: {
    ttl_followup_enabled: boolean;
  };
}

interface FormData {
  name: string;
  provider: string;
  api_key: string;
  base_url: string;
  use_v1_suffix: boolean;
  model: string;
  note: string;
}

interface CheckInFormData {
  name: string;
  label: string;
  enabled: boolean;
  schedule_type: 'window' | 'after_ai_call';
  time_start: string;
  time_end: string;
  days_of_week: string;
  interval_min_minutes: string;
  interval_max_minutes: string;
  prompt_template: string;
  instructions: string;
  tool_profile: 'poll' | 'reminder_safe' | 'none';
  allow_silent: boolean;
  context_config: Record<string, boolean>;
}

const PROVIDERS = ['claude', 'openai', 'relay', 'gemini'] as const;
const CHECK_IN_CONTEXT_KEYS = [
  'include_projects',
  'include_memories',
  'include_relevant_history',
  'include_today_timeline',
  'include_pending_reminders',
  'include_deadlines',
  'include_weather',
  'include_calendar',
] as const;

const emptyForm: FormData = {
  name: '',
  provider: 'claude',
  api_key: '',
  base_url: '',
  use_v1_suffix: true,
  model: '',
  note: '',
};

async function postJson(url: string, body: unknown) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

async function patchJson(url: string, body: unknown) {
  const r = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

async function delJson(url: string) {
  const r = await fetch(url, { method: 'DELETE' });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

async function putJson(url: string, body: unknown) {
  const r = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

const PROMPT_HINTS: Record<string, string> = {
  main_template: '完整 system prompt：人格/规则散文直接写在这里，系统知识用占位符注入。',
  tools: '跨工具决策和各工具使用策略；经 {tools} 注入主模板（工具多轮的中间轮自动省略）。',
  proactive_gemini: 'Gemini 主动轮询模板，必须保留 {timestamp}。',
  proactive_claude: 'Claude/OpenAI/Relay 主动轮询模板，必须保留 {timestamp}。',
  reminder: 'Reminder 到点后的跟进模板，必须保留 {timestamp} 和 {action}。',
  bedtime: '睡前提醒模板，必须保留 {timestamp}。',
  morning: '早间开启模板；当前仅供后续调用点使用。',
  weather_report: '天气总结模板，必须保留 {weather_data}。',
};

// main_template 里可用的占位符：名称 → 说明。顺序 = 推荐排列
// （稳定 → 易变，保持 prompt cache 前缀命中率）。
const PLACEHOLDER_DOCS: [string, string][] = [
  ['tools', '工具使用策略（下方可单独编辑）'],
  ['projects', '现有项目列表'],
  ['memories', '长期记忆（你现在记着的事）'],
  ['relevant_history', '语义检索的历史对话片段'],
  ['today_timeline', '今天完整 timeline'],
  ['pending_reminders', '待触发的 reminder 队列'],
  ['deadlines', '待完成的 deadline'],
  ['weather', '今日天气（仅早晨注入）'],
  ['calendar', 'Google Calendar 今天+7天'],
];

export function AdminPanel() {
  const [tab, setTab] = useState<'presets' | 'prompts' | 'checkins'>('presets');
  const [data, setData] = useState<PresetsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [editor, setEditor] = useState<{ mode: 'add' | 'edit'; original?: Preset } | null>(null);

  const load = async () => {
    try {
      const r = await fetch('/api/admin/presets');
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      setData(await r.json());
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  };

  useEffect(() => { load(); }, []);

  const setActive = async (name: string) => {
    try { await postJson('/api/admin/presets/active', { name }); await load(); }
    catch (e) { setErr(String(e)); }
  };

  const setFallback = async (name: string | null) => {
    try { await postJson('/api/admin/presets/fallback', { name }); await load(); }
    catch (e) { setErr(String(e)); }
  };

  const test = async (name: string) => {
    setBusy(name);
    try {
      const j: TestResult = await postJson('/api/admin/presets/test', { name });
      setResults(prev => ({ ...prev, [name]: j }));
    } catch (e) {
      setResults(prev => ({
        ...prev,
        [name]: { ok: false, error: String(e), latency_ms: 0, provider: '', model: '' },
      }));
    } finally {
      setBusy(null);
    }
  };

  const saveNote = async (name: string, note: string) => {
    try { await patchJson(`/api/admin/presets/${encodeURIComponent(name)}`, { note }); await load(); }
    catch (e) { setErr(String(e)); }
  };

  const handleDelete = async (name: string) => {
    if (!window.confirm(`Delete preset "${name}"?\nThis cannot be undone.`)) return;
    try { await delJson(`/api/admin/presets/${encodeURIComponent(name)}`); await load(); }
    catch (e) { setErr(String(e)); }
  };

  const handleSubmit = async (form: FormData) => {
    if (editor?.mode === 'add') {
      await postJson('/api/admin/presets', form);
    } else if (editor?.mode === 'edit' && editor.original) {
      const body: Partial<FormData> = {
        provider: form.provider,
        base_url: form.base_url,
        use_v1_suffix: form.use_v1_suffix,
        model: form.model,
        note: form.note,
      };
      // 编辑时 api_key 留空 = 不改动；填了才传
      if (form.api_key.trim()) body.api_key = form.api_key;
      await patchJson(`/api/admin/presets/${encodeURIComponent(editor.original.name)}`, body);
    }
    setEditor(null);
    await load();
  };

  return (
    <div className="admin-panel">
      <header className="admin-header">
        <div>
          <h1>Admin</h1>
          <div className="admin-tabs">
            <button className={tab === 'presets' ? 'active' : ''}
                    onClick={() => setTab('presets')}>Presets</button>
            <button className={tab === 'prompts' ? 'active' : ''}
                    onClick={() => setTab('prompts')}>Prompts</button>
            <button className={tab === 'checkins' ? 'active' : ''}
                    onClick={() => setTab('checkins')}>Check-ins</button>
          </div>
        </div>
        <div className="admin-actions">
          {tab === 'presets' && (
            <button className="admin-btn primary" onClick={() => setEditor({ mode: 'add' })}>
              + Add preset
            </button>
          )}
          {tab === 'presets' && <button className="admin-btn" onClick={load}>Refresh</button>}
          <button className="admin-btn" onClick={() => { window.location.hash = ''; }}>
            ← Back
          </button>
        </div>
      </header>

      {tab === 'prompts' ? (
        <PromptAdmin />
      ) : tab === 'checkins' ? (
        <CheckInAdmin />
      ) : !data ? (
        <div className={`admin-msg ${err ? 'err' : ''}`}>
          {err ? `加载失败: ${err}` : '加载中…'}
        </div>
      ) : (
        <>
      <div className="admin-summary">
        <span className="badge"><b>Active</b>{data.active ?? '—'}</span>
        <span className="badge"><b>Fallback</b>{data.fallback ?? '—'}</span>
      </div>

      {err && <div className="admin-msg err">{err}</div>}

      <table className="admin-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Provider</th>
            <th>Model</th>
            <th>URL</th>
            <th>API key</th>
            <th>Note</th>
            <th>State</th>
            <th>Actions</th>
            <th>Test (hello)</th>
          </tr>
        </thead>
        <tbody>
          {data.presets.map(p => {
            const isActive = p.name === data.active;
            const isFallback = p.name === data.fallback;
            const r = results[p.name];
            return (
              <tr key={p.name}>
                <td className="name">{p.name}</td>
                <td>{p.provider}</td>
                <td className="mono">{p.model}</td>
                <td className="mono dim">{p.use_v1_suffix ? '+ /v1' : 'direct'}</td>
                <td className="mono dim">{p.api_key_masked || '—'}</td>
                <td className="note-cell">
                  <NoteInput value={p.note} onSave={(v) => saveNote(p.name, v)} />
                </td>
                <td>
                  {isActive && <span className="tag tag-active">主</span>}
                  {isFallback && <span className="tag tag-fallback">备</span>}
                </td>
                <td className="actions">
                  <button className="admin-btn" disabled={isActive}
                          onClick={() => setActive(p.name)}>Set active</button>
                  <button className="admin-btn" disabled={isActive}
                          onClick={() => setFallback(isFallback ? null : p.name)}>
                    {isFallback ? 'Clear fallback' : 'Set fallback'}
                  </button>
                  <button className="admin-btn"
                          onClick={() => setEditor({ mode: 'edit', original: p })}>
                    Edit
                  </button>
                  <button className="admin-btn warn" disabled={isActive}
                          onClick={() => handleDelete(p.name)}>Delete</button>
                </td>
                <td>
                  <button className="admin-btn" disabled={busy === p.name}
                          onClick={() => test(p.name)}>
                    {busy === p.name ? 'Testing…' : 'Test'}
                  </button>
                  {r && (
                    <div className={`test-result ${r.ok ? 'ok' : 'err'}`}>
                      <div className="meta">
                        {r.ok ? '✓' : '✗'} {r.latency_ms}ms · {r.provider}/{r.model}
                      </div>
                      <pre>{r.ok ? (r.reply ?? '') : (r.error ?? 'unknown error')}</pre>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {editor && (
        <PresetEditor
          mode={editor.mode}
          original={editor.original}
          onCancel={() => setEditor(null)}
          onSubmit={handleSubmit}
        />
      )}
        </>
      )}
    </div>
  );
}

function fmtSchedule(checkIn: CheckIn) {
  if (checkIn.schedule_type === 'after_ai_call') {
    const min = checkIn.interval_min_minutes ?? 45;
    const max = checkIn.interval_max_minutes ?? min;
    return `after AI call + ${min}-${max}m`;
  }
  const time = checkIn.time_start === checkIn.time_end
    ? (checkIn.time_start ?? '—')
    : `${checkIn.time_start ?? '—'}-${checkIn.time_end ?? '—'}`;
  if (!checkIn.days_of_week || checkIn.days_of_week.length === 0) return `daily ${time}`;
  return `${checkIn.days_of_week.join(',')} · ${time}`;
}

function CheckInAdmin() {
  const [data, setData] = useState<CheckInsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ mode: 'add' | 'edit'; original?: CheckIn } | null>(null);

  const load = async () => {
    try {
      const r = await fetch('/api/check-ins');
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      setData(await r.json());
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  };

  useEffect(() => { load(); }, []);

  const patchCheckIn = async (id: number, body: unknown) => {
    setBusy(String(id));
    try {
      await patchJson(`/api/check-ins/${id}`, body);
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  };

  const setTtl = async (enabled: boolean) => {
    setBusy('ttl');
    try {
      await patchJson('/api/settings/check-ins', { ttl_followup_enabled: enabled });
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  };

  const saveCheckIn = async (form: CheckInFormData) => {
    const body = checkInFormToBody(form);
    if (editor?.mode === 'edit' && editor.original) {
      await patchJson(`/api/check-ins/${editor.original.id}`, body);
    } else {
      await postJson('/api/check-ins', body);
    }
    setEditor(null);
    await load();
  };

  const deleteCheckIn = async (checkIn: CheckIn) => {
    if (checkIn.built_in) return;
    if (!window.confirm(`Delete check-in "${checkIn.name}"?`)) return;
    setBusy(String(checkIn.id));
    try {
      await delJson(`/api/check-ins/${checkIn.id}`);
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  };

  if (err && !data) return <div className="admin-msg err">加载失败: {err}</div>;
  if (!data) return <div className="admin-msg">加载中…</div>;

  return (
    <div className="checkin-admin">
      {err && <div className="admin-msg err">{err}</div>}

      <div className="admin-summary">
        <span className="badge"><b>TTL follow-up</b>{data.settings.ttl_followup_enabled ? 'on' : 'off'}</span>
        <button
          className="admin-btn"
          disabled={busy === 'ttl'}
          onClick={() => setTtl(!data.settings.ttl_followup_enabled)}
        >
          {data.settings.ttl_followup_enabled ? 'Turn off TTL' : 'Turn on TTL'}
        </button>
        <button className="admin-btn primary" onClick={() => setEditor({ mode: 'add' })}>
          + Add check-in
        </button>
        <button className="admin-btn" onClick={load}>Refresh</button>
      </div>

      <table className="admin-table checkin-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Enabled</th>
            <th>Schedule</th>
            <th>Profile</th>
            <th>Silent</th>
            <th>Last fired</th>
            <th>Next picked</th>
            <th>Prompt</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.check_ins.map(checkIn => (
            <tr key={checkIn.id}>
              <td>
                <div className="name">{checkIn.label}</div>
                <div className="mono dim">{checkIn.name}{checkIn.built_in ? ' · built-in' : ''}</div>
              </td>
              <td>
                <button
                  className={`admin-btn ${checkIn.enabled ? 'primary' : ''}`}
                  disabled={busy === String(checkIn.id)}
                  onClick={() => patchCheckIn(checkIn.id, { enabled: !checkIn.enabled })}
                >
                  {checkIn.enabled ? 'On' : 'Off'}
                </button>
              </td>
              <td>{fmtSchedule(checkIn)}</td>
              <td><span className="tag tag-fallback">{checkIn.tool_profile}</span></td>
              <td>{checkIn.allow_silent ? 'prompt-driven' : 'off'}</td>
              <td className="mono dim">{checkIn.last_fired_at ?? '—'}</td>
              <td className="mono dim">{checkIn.last_scheduled_for ?? '—'}</td>
              <td className="checkin-prompt">
                {checkIn.prompt_template.slice(0, 140)}
                {checkIn.prompt_template.length > 140 ? '…' : ''}
              </td>
              <td className="actions">
                <button className="admin-btn" onClick={() => setEditor({ mode: 'edit', original: checkIn })}>
                  Edit
                </button>
                <button
                  className="admin-btn warn"
                  disabled={checkIn.built_in || busy === String(checkIn.id)}
                  onClick={() => deleteCheckIn(checkIn)}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {editor && (
        <CheckInEditor
          mode={editor.mode}
          original={editor.original}
          onCancel={() => setEditor(null)}
          onSubmit={saveCheckIn}
        />
      )}
    </div>
  );
}

function defaultContextConfig() {
  return Object.fromEntries(CHECK_IN_CONTEXT_KEYS.map(key => [key, true])) as Record<string, boolean>;
}

function checkInToForm(checkIn?: CheckIn): CheckInFormData {
  if (!checkIn) {
    return {
      name: '',
      label: '',
      enabled: true,
      schedule_type: 'window',
      time_start: '09:00',
      time_end: '09:15',
      days_of_week: '',
      interval_min_minutes: '45',
      interval_max_minutes: '55',
      prompt_template: 'Current timestamp: {timestamp}\n\n{instructions}',
      instructions: '',
      tool_profile: 'poll',
      allow_silent: true,
      context_config: defaultContextConfig(),
    };
  }
  return {
    name: checkIn.name,
    label: checkIn.label,
    enabled: checkIn.enabled,
    schedule_type: checkIn.schedule_type,
    time_start: checkIn.time_start ?? '09:00',
    time_end: checkIn.time_end ?? checkIn.time_start ?? '09:15',
    days_of_week: checkIn.days_of_week?.join(',') ?? '',
    interval_min_minutes: String(checkIn.interval_min_minutes ?? 45),
    interval_max_minutes: String(checkIn.interval_max_minutes ?? checkIn.interval_min_minutes ?? 55),
    prompt_template: checkIn.prompt_template,
    instructions: checkIn.instructions ?? '',
    tool_profile: checkIn.tool_profile,
    allow_silent: checkIn.allow_silent,
    context_config: { ...defaultContextConfig(), ...(checkIn.context_config ?? {}) },
  };
}

function checkInFormToBody(form: CheckInFormData) {
  const days = form.days_of_week.trim()
    ? form.days_of_week.split(',').map(v => Number(v.trim())).filter(v => !Number.isNaN(v))
    : null;
  return {
    name: form.name.trim(),
    label: form.label.trim() || form.name.trim(),
    enabled: form.enabled,
    schedule_type: form.schedule_type,
    time_start: form.schedule_type === 'window' ? form.time_start : null,
    time_end: form.schedule_type === 'window' ? form.time_end : null,
    days_of_week: form.schedule_type === 'window' ? days : null,
    interval_min_minutes: form.schedule_type === 'after_ai_call' ? form.interval_min_minutes : null,
    interval_max_minutes: form.schedule_type === 'after_ai_call' ? form.interval_max_minutes : null,
    prompt_template: form.prompt_template,
    instructions: form.instructions,
    tool_profile: form.tool_profile,
    allow_silent: form.allow_silent,
    context_config: form.context_config,
  };
}

function CheckInEditor({
  mode, original, onCancel, onSubmit,
}: {
  mode: 'add' | 'edit';
  original?: CheckIn;
  onCancel: () => void;
  onSubmit: (form: CheckInFormData) => Promise<void>;
}) {
  const [form, setForm] = useState<CheckInFormData>(() => checkInToForm(original));
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const update = <K extends keyof CheckInFormData>(key: K, value: CheckInFormData[K]) => {
    setForm(prev => ({ ...prev, [key]: value }));
  };
  const updateContext = (key: string, checked: boolean) => {
    setForm(prev => ({ ...prev, context_config: { ...prev.context_config, [key]: checked } }));
  };
  const submit = async () => {
    setSubmitting(true);
    setErr(null);
    try {
      await onSubmit(form);
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-card checkin-editor-card" onClick={(e) => e.stopPropagation()}>
        <h2>{mode === 'add' ? 'Add check-in' : `Edit · ${original?.name}`}</h2>

        <div className="form-grid two">
          <label className="form-row">
            <span>Name</span>
            <input type="text" value={form.name} disabled={mode === 'edit'}
                   onChange={(e) => update('name', e.target.value)} />
          </label>
          <label className="form-row">
            <span>Label</span>
            <input type="text" value={form.label}
                   onChange={(e) => update('label', e.target.value)} />
          </label>
        </div>

        <div className="form-grid two">
          <label className="form-row">
            <span>Schedule</span>
            <select value={form.schedule_type}
                    onChange={(e) => update('schedule_type', e.target.value as CheckInFormData['schedule_type'])}>
              <option value="window">Time window</option>
              <option value="after_ai_call">After AI call</option>
            </select>
          </label>
          <label className="form-row">
            <span>Tool profile</span>
            <select value={form.tool_profile}
                    onChange={(e) => update('tool_profile', e.target.value as CheckInFormData['tool_profile'])}>
              <option value="poll">poll</option>
              <option value="reminder_safe">reminder_safe</option>
              <option value="none">none</option>
            </select>
          </label>
        </div>

        {form.schedule_type === 'window' ? (
          <div className="form-grid three">
            <label className="form-row">
              <span>Start</span>
              <input type="time" value={form.time_start}
                     onChange={(e) => update('time_start', e.target.value)} />
            </label>
            <label className="form-row">
              <span>End</span>
              <input type="time" value={form.time_end}
                     onChange={(e) => update('time_end', e.target.value)} />
            </label>
            <label className="form-row">
              <span>Days</span>
              <input type="text" value={form.days_of_week}
                     placeholder="blank = daily, or 0,1,2"
                     onChange={(e) => update('days_of_week', e.target.value)} />
            </label>
          </div>
        ) : (
          <div className="form-grid two">
            <label className="form-row">
              <span>Min minutes</span>
              <input type="number" min="1" value={form.interval_min_minutes}
                     onChange={(e) => update('interval_min_minutes', e.target.value)} />
            </label>
            <label className="form-row">
              <span>Max minutes</span>
              <input type="number" min="1" value={form.interval_max_minutes}
                     onChange={(e) => update('interval_max_minutes', e.target.value)} />
            </label>
          </div>
        )}

        <div className="form-grid two">
          <label className="form-row check-row">
            <span>Enabled</span>
            <label className="check-control">
              <input type="checkbox" checked={form.enabled}
                     onChange={(e) => update('enabled', e.target.checked)} />
              <span>Run this check-in</span>
            </label>
          </label>
          <label className="form-row check-row">
            <span>Silent</span>
            <label className="check-control">
              <input type="checkbox" checked={form.allow_silent}
                     onChange={(e) => update('allow_silent', e.target.checked)} />
              <span>Allow [SILENT]</span>
            </label>
          </label>
        </div>

        <label className="form-row">
          <span>Instructions</span>
          <input type="text" value={form.instructions}
                 placeholder="available as {instructions}"
                 onChange={(e) => update('instructions', e.target.value)} />
        </label>

        <label className="form-row">
          <span>Prompt template</span>
          <textarea className="form-textarea" value={form.prompt_template}
                    spellCheck={false}
                    onChange={(e) => update('prompt_template', e.target.value)} />
          <span className="form-hint">Available placeholders: {'{timestamp}'}, {'{name}'}, {'{label}'}, {'{instructions}'}</span>
        </label>

        <div className="context-grid">
          {CHECK_IN_CONTEXT_KEYS.map(key => (
            <label key={key} className="check-control">
              <input type="checkbox" checked={form.context_config[key] ?? true}
                     onChange={(e) => updateContext(key, e.target.checked)} />
              <span>{key.replace('include_', '')}</span>
            </label>
          ))}
        </div>

        {err && <div className="form-err">{err}</div>}

        <div className="modal-actions">
          <button className="admin-btn" onClick={onCancel} disabled={submitting}>Cancel</button>
          <button className="admin-btn primary" onClick={submit}
                  disabled={submitting || !form.name.trim() || !form.prompt_template.trim()}>
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

function PromptAdmin() {
  const [data, setData] = useState<PromptsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await fetch('/api/admin/prompts');
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      setData(await r.json());
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  };

  useEffect(() => { load(); }, []);

  if (err && !data) return <div className="admin-msg err">加载失败: {err}</div>;
  if (!data) return <div className="admin-msg">加载中…</div>;

  const main = data.sections.find(s => s.key === 'main_template');
  const others = data.sections.filter(s => !s.hidden && s.key !== 'main_template');

  return (
    <div className="prompt-admin-page">
      {err && <div className="admin-msg err">{err}</div>}
      {main
        ? <MainTemplateEditor section={main} onSaved={load} />
        : <div className="admin-msg err">main_template section 不存在（后端未迁移？）</div>}

      <details className="prompt-others">
        <summary>其他模板（tools / 调度小模板 / dispatch）</summary>
        <SectionListEditor sections={others} onSaved={load} />
      </details>
    </div>
  );
}

function MainTemplateEditor({ section, onSaved }: {
  section: PromptSection;
  onSaved: () => Promise<void>;
}) {
  const [draft, setDraft] = useState(section.current_value);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { setDraft(section.current_value); }, [section.current_value]);

  const dirty = draft !== section.current_value;
  const missingTools = !draft.includes('{tools}');

  const insertAtCursor = (token: string) => {
    const ta = taRef.current;
    const start = ta?.selectionStart ?? draft.length;
    const end = ta?.selectionEnd ?? start;
    setDraft(draft.slice(0, start) + token + draft.slice(end));
    requestAnimationFrame(() => {
      if (!ta) return;
      ta.focus();
      const pos = start + token.length;
      ta.setSelectionRange(pos, pos);
    });
  };

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      await putJson('/api/admin/prompts/main_template', { value: draft });
      await onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="prompt-editor prompt-main-editor">
      <div className="prompt-editor-head">
        <div>
          <h2>整体 Prompt 模板</h2>
          <p>{PROMPT_HINTS.main_template}</p>
        </div>
        <div className="prompt-editor-actions">
          <button className="admin-btn" onClick={onSaved} disabled={saving}>Reload</button>
          <button className="admin-btn primary" onClick={save}
                  disabled={saving || !dirty || !draft.trim()}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {err && <div className="admin-msg err">{err}</div>}

      <div className="placeholder-bar">
        {PLACEHOLDER_DOCS.map(([name, doc]) => {
          const token = `{${name}}`;
          const included = draft.includes(token);
          return (
            <button
              key={name}
              type="button"
              className={`placeholder-chip ${included ? 'included' : ''}`}
              title={`${doc}${included ? '' : '（未使用，点击在光标处插入）'}`}
              onClick={() => !included && insertAtCursor(token)}
            >
              {token}
            </button>
          );
        })}
      </div>
      {missingTools && (
        <div className="placeholder-warn">
          ⚠ 模板里没有 {'{tools}'}：AI 将看不到任何工具使用策略（可以保存，但请确认是有意为之）
        </div>
      )}

      <div className="prompt-meta">
        <span className={`tag ${section.empty ? 'tag-fallback' : 'tag-active'}`}>
          {section.empty ? 'empty' : 'DB managed'}
        </span>
        <span>{draft.length.toLocaleString()} chars</span>
        {section.updated_at && <span>updated {section.updated_at}</span>}
        {dirty && <span>unsaved changes</span>}
      </div>

      <textarea
        ref={taRef}
        className="prompt-textarea prompt-main-textarea"
        value={draft}
        spellCheck={false}
        onChange={(e) => setDraft(e.target.value)}
      />
    </main>
  );
}

function SectionListEditor({ sections, onSaved }: {
  sections: PromptSection[];
  onSaved: () => Promise<void>;
}) {
  const [selectedKey, setSelectedKey] = useState<string>('');
  const [draft, setDraft] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const selected = sections.find(s => s.key === selectedKey) ?? sections[0];

  useEffect(() => {
    if (selected) setDraft(selected.current_value);
  }, [selected?.key, selected?.current_value]);

  const save = async () => {
    if (!selected) return;
    setSaving(true);
    setErr(null);
    try {
      await putJson(`/api/admin/prompts/${encodeURIComponent(selected.key)}`, { value: draft });
      await onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (!selected) return <div className="admin-msg">没有可编辑的 section</div>;

  const dirty = draft !== selected.current_value;

  return (
    <div className="prompt-admin">
      {err && <div className="admin-msg err">{err}</div>}

      <aside className="prompt-list">
        {sections.map(section => (
          <button
            key={section.key}
            className={section.key === selected.key ? 'active' : ''}
            onClick={() => setSelectedKey(section.key)}
          >
            <span>{section.label}</span>
            {section.empty && <b>empty</b>}
          </button>
        ))}
      </aside>

      <main className="prompt-editor">
        <div className="prompt-editor-head">
          <div>
            <h2>{selected.label}</h2>
            <p>{PROMPT_HINTS[selected.key] || selected.key}</p>
          </div>
          <div className="prompt-editor-actions">
            <button className="admin-btn" onClick={onSaved} disabled={saving}>Reload</button>
            <button className="admin-btn primary" onClick={save}
                    disabled={saving || !dirty || !draft.trim()}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>

        <div className="prompt-meta">
          <span className={`tag ${selected.empty ? 'tag-fallback' : 'tag-active'}`}>
            {selected.empty ? 'empty' : 'DB managed'}
          </span>
          <span>{draft.length.toLocaleString()} chars</span>
          {selected.updated_at && <span>updated {selected.updated_at}</span>}
          {dirty && <span>unsaved changes</span>}
        </div>

        <textarea
          className="prompt-textarea"
          value={draft}
          spellCheck={false}
          onChange={(e) => setDraft(e.target.value)}
        />
      </main>
    </div>
  );
}

function NoteInput({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value);
  useEffect(() => { setV(value); }, [value]);
  return (
    <input
      className="note-input"
      type="text"
      value={v}
      placeholder="—"
      onChange={(e) => setV(e.target.value)}
      onBlur={() => { if (v !== value) onSave(v); }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
        if (e.key === 'Escape') { setV(value); (e.target as HTMLInputElement).blur(); }
      }}
    />
  );
}

function PresetEditor({
  mode, original, onCancel, onSubmit,
}: {
  mode: 'add' | 'edit';
  original?: Preset;
  onCancel: () => void;
  onSubmit: (form: FormData) => Promise<void>;
}) {
  const [form, setForm] = useState<FormData>(() => {
    if (mode === 'edit' && original) {
      return {
        name: original.name,
        provider: original.provider,
        api_key: '',
        base_url: original.base_url || '',
        use_v1_suffix: original.use_v1_suffix ?? true,
        model: original.model,
        note: original.note || '',
      };
    }
    return { ...emptyForm };
  });
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const update = <K extends keyof FormData>(k: K, v: FormData[K]) => {
    setForm(prev => ({ ...prev, [k]: v }));
  };

  const submit = async () => {
    setSubmitting(true);
    setErr(null);
    try {
      await onSubmit(form);
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h2>{mode === 'add' ? 'Add preset' : `Edit · ${original?.name}`}</h2>

        <label className="form-row">
          <span>Name</span>
          <input type="text" value={form.name} disabled={mode === 'edit'}
                 placeholder="unique identifier"
                 onChange={(e) => update('name', e.target.value)} />
          {mode === 'edit' && <span className="form-hint">改名不支持，要改请删了重建</span>}
        </label>

        <label className="form-row">
          <span>Provider</span>
          <select value={form.provider} onChange={(e) => update('provider', e.target.value)}>
            {PROVIDERS.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>

        <label className="form-row">
          <span>Model</span>
          <input type="text" value={form.model}
                 placeholder="e.g. claude-opus-4-6"
                 onChange={(e) => update('model', e.target.value)} />
        </label>

        <label className="form-row">
          <span>Base URL</span>
          <input type="text" value={form.base_url}
                 placeholder={form.provider === 'relay' ? 'required' : 'optional'}
                 onChange={(e) => update('base_url', e.target.value)} />
        </label>

        <label className="form-row check-row">
          <span>Relay URL</span>
          <label className="check-control">
            <input type="checkbox" checked={form.use_v1_suffix}
                   onChange={(e) => update('use_v1_suffix', e.target.checked)} />
            <span>Use /v1 suffix before /chat/completions</span>
          </label>
          <span className="form-hint">
            勾选：base_url + /v1/chat/completions；不勾：base_url + /chat/completions
          </span>
        </label>

        <label className="form-row">
          <span>API key</span>
          <input type="password" value={form.api_key}
                 placeholder={mode === 'edit' ? '(leave empty to keep current)' : 'required'}
                 onChange={(e) => update('api_key', e.target.value)} />
        </label>

        <label className="form-row">
          <span>Note</span>
          <input type="text" value={form.note}
                 placeholder="optional, free-form"
                 onChange={(e) => update('note', e.target.value)} />
        </label>

        {err && <div className="form-err">{err}</div>}

        <div className="modal-actions">
          <button className="admin-btn" onClick={onCancel} disabled={submitting}>Cancel</button>
          <button className="admin-btn primary" onClick={submit} disabled={submitting}>
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
