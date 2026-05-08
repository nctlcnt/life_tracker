import { useEffect, useState } from 'react';
import './admin.css';

interface Preset {
  name: string;
  provider: string;
  model: string;
  base_url: string;
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

async function postJson(url: string, body: unknown) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status}: ${text}`);
  }
  return r.json();
}

export function AdminPanel() {
  const [data, setData] = useState<PresetsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, TestResult>>({});

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
    try {
      await postJson('/api/admin/presets/active', { name });
      await load();
    } catch (e) {
      setErr(String(e));
    }
  };

  const setFallback = async (name: string | null) => {
    try {
      await postJson('/api/admin/presets/fallback', { name });
      await load();
    } catch (e) {
      setErr(String(e));
    }
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
          <button className="admin-btn" onClick={load}>Refresh</button>
          <button
            className="admin-btn"
            onClick={() => { window.location.hash = ''; }}
          >
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
                <td>
                  {isActive && <span className="tag tag-active">主</span>}
                  {isFallback && <span className="tag tag-fallback">备</span>}
                </td>
                <td className="actions">
                  <button
                    className="admin-btn"
                    disabled={isActive}
                    onClick={() => setActive(p.name)}
                  >
                    Set active
                  </button>
                  <button
                    className="admin-btn"
                    disabled={isActive}
                    onClick={() => setFallback(isFallback ? null : p.name)}
                  >
                    {isFallback ? 'Clear fallback' : 'Set fallback'}
                  </button>
                </td>
                <td>
                  <button
                    className="admin-btn"
                    disabled={busy === p.name}
                    onClick={() => test(p.name)}
                  >
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
    </div>
  );
}
