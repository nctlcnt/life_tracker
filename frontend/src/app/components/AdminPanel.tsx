import { useEffect, useState } from 'react';
import './admin.css';

interface Preset {
  name: string;
  provider: string;
  model: string;
  base_url: string;
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

interface FormData {
  name: string;
  provider: string;
  api_key: string;
  base_url: string;
  model: string;
  note: string;
}

const PROVIDERS = ['claude', 'openai', 'relay', 'gemini'] as const;

const emptyForm: FormData = {
  name: '',
  provider: 'claude',
  api_key: '',
  base_url: '',
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

export function AdminPanel() {
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

  if (err && !data) {
    return <div className="admin-panel"><div className="admin-msg err">加载失败: {err}</div></div>;
  }
  if (!data) {
    return <div className="admin-panel"><div className="admin-msg">加载中…</div></div>;
  }

  return (
    <div className="admin-panel">
      <header className="admin-header">
        <h1>Admin · Presets</h1>
        <div className="admin-actions">
          <button className="admin-btn primary" onClick={() => setEditor({ mode: 'add' })}>
            + Add preset
          </button>
          <button className="admin-btn" onClick={load}>Refresh</button>
          <button className="admin-btn" onClick={() => { window.location.hash = ''; }}>
            ← Back
          </button>
        </div>
      </header>

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
