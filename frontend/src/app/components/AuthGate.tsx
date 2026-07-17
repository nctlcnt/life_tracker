import { FormEvent, ReactNode, useEffect, useState } from 'react';

type SessionResponse = {
  authenticated: boolean;
  configured: boolean;
};

export function AuthGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    fetch('/api/auth/session')
      .then(async response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then(setSession)
      .catch(() => setError('无法连接到 Life Tracker API'));
  }, []);

  async function login(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${response.status}`);
      }
      setApiKey('');
      setSession({ authenticated: true, configured: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      setSubmitting(false);
    }
  }

  async function logout() {
    await fetch('/api/auth/logout', { method: 'POST' }).catch(() => undefined);
    setSession(current => ({ authenticated: false, configured: current?.configured ?? true }));
  }

  if (session?.authenticated) {
    return (
      <>
        {children}
        <button className="auth-logout" onClick={logout} type="button">退出</button>
      </>
    );
  }

  const loading = session === null && !error;
  return (
    <main className="auth-page">
      <section className="auth-card" aria-busy={loading}>
        <div className="auth-mark">日和</div>
        <p className="auth-kicker">LIFE TRACKER</p>
        <h1>私人仪表盘</h1>
        <p className="auth-copy">
          {loading
            ? '正在检查登录状态…'
            : session?.configured === false
              ? '服务器尚未配置 API 密钥。'
              : '输入 API 密钥以继续。密钥只用于本次登录，不会保存在浏览器存储中。'}
        </p>
        {!loading && session?.configured !== false && (
          <form onSubmit={login} className="auth-form">
            <label htmlFor="api-key">API 密钥</label>
            <input
              id="api-key"
              type="password"
              value={apiKey}
              onChange={event => setApiKey(event.target.value)}
              autoComplete="current-password"
              autoFocus
              required
            />
            <button type="submit" disabled={submitting || !apiKey}>
              {submitting ? '验证中…' : '进入仪表盘'}
            </button>
          </form>
        )}
        {error && <p className="auth-error" role="alert">{error}</p>}
      </section>
    </main>
  );
}
