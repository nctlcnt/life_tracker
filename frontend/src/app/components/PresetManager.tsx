import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { Plus, Pencil, Trash2, Check, X, Eye, EyeOff, KeyRound, Server, Cpu, FileText } from 'lucide-react';

interface Preset {
  name: string;
  provider: string;
  api_key: string;
  base_url: string;
  model: string;
  notes: string;
}

interface PresetsResponse {
  presets: Preset[];
  active: string;
  fallback: string | null;
  supported_providers: string[];
}

type EditState =
  | { kind: 'none' }
  | { kind: 'create' }
  | { kind: 'edit'; name: string };

const EMPTY_FORM: Preset = {
  name: '',
  provider: 'claude',
  api_key: '',
  base_url: '',
  model: '',
  notes: '',
};

function maskKey(k: string): string {
  if (!k) return '(未设置)';
  if (k.length <= 8) return '••••';
  return `${k.slice(0, 4)}…${k.slice(-4)}`;
}

export function PresetManager() {
  const [data, setData] = useState<PresetsResponse | null>(null);
  const [edit, setEdit] = useState<EditState>({ kind: 'none' });
  const [form, setForm] = useState<Preset>(EMPTY_FORM);
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const refresh = async () => {
    try {
      const res = await fetch('/api/presets');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as PresetsResponse;
      setData(json);
    } catch (err) {
      console.error('Failed to load presets:', err);
      setError(`加载失败: ${err}`);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const startCreate = () => {
    setForm({ ...EMPTY_FORM, provider: data?.supported_providers?.[0] ?? 'claude' });
    setEdit({ kind: 'create' });
    setError(null);
  };

  const startEdit = (p: Preset) => {
    setForm({ ...p });
    setEdit({ kind: 'edit', name: p.name });
    setError(null);
  };

  const cancel = () => {
    setEdit({ kind: 'none' });
    setForm(EMPTY_FORM);
    setError(null);
  };

  const save = async () => {
    setError(null);
    setPending(true);
    try {
      let res: Response;
      if (edit.kind === 'create') {
        res = await fetch('/api/presets', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(form),
        });
      } else if (edit.kind === 'edit') {
        const { name: _name, ...rest } = form;
        res = await fetch(`/api/presets/${encodeURIComponent(edit.name)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(rest),
        });
      } else {
        return;
      }
      if (!res.ok) {
        const detail = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(detail.detail || `HTTP ${res.status}`);
      }
      await refresh();
      cancel();
    } catch (err: any) {
      setError(err?.message ?? String(err));
    } finally {
      setPending(false);
    }
  };

  const remove = async (name: string) => {
    if (!confirm(`确认删除 preset "${name}"？此操作会同步写回 config.json。`)) return;
    setError(null);
    try {
      const res = await fetch(`/api/presets/${encodeURIComponent(name)}`, { method: 'DELETE' });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(detail.detail || `HTTP ${res.status}`);
      }
      await refresh();
    } catch (err: any) {
      setError(err?.message ?? String(err));
    }
  };

  const setActive = async (name: string) => {
    try {
      const res = await fetch('/api/presets/active', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await refresh();
    } catch (err: any) {
      setError(err?.message ?? String(err));
    }
  };

  const setFallback = async (name: string | null) => {
    try {
      const res = await fetch('/api/presets/fallback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await refresh();
    } catch (err: any) {
      setError(err?.message ?? String(err));
    }
  };

  const toggleReveal = (name: string) => {
    setRevealed(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  if (!data) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
        加载中…
      </div>
    );
  }

  const providers = data.supported_providers;

  return (
    <div className="flex-1 overflow-auto px-8 py-6">
      <div className="max-w-3xl mx-auto">
        {/* 标题 + 添加按钮 */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-foreground" style={{ fontSize: '1.25rem', fontWeight: 500 }}>
              API Preset 管理
            </h2>
            <p className="text-sm text-muted-foreground mt-1">
              管理 AI 服务商配置 · 修改即时写回 config.json
            </p>
          </div>
          {edit.kind === 'none' && (
            <button
              onClick={startCreate}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-foreground text-background text-sm hover:opacity-90 transition-opacity"
            >
              <Plus className="w-4 h-4" />
              添加 preset
            </button>
          )}
        </div>

        {error && (
          <div className="mb-4 px-4 py-3 rounded-md bg-destructive/10 border border-destructive/30 text-destructive text-sm">
            {error}
          </div>
        )}

        {/* 添加/编辑表单（在顶部展开） */}
        <AnimatePresence>
          {edit.kind !== 'none' && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.15 }}
              className="mb-6 overflow-hidden"
            >
              <div className="bg-card border border-border rounded-lg p-5">
                <h3 className="mb-4 text-foreground" style={{ fontSize: '1rem', fontWeight: 500 }}>
                  {edit.kind === 'create' ? '添加 preset' : `编辑 ${edit.name}`}
                </h3>
                <div className="grid grid-cols-2 gap-4">
                  <FormField label="名称" required disabled={edit.kind === 'edit'}>
                    <input
                      type="text"
                      value={form.name}
                      onChange={e => setForm({ ...form, name: e.target.value })}
                      disabled={edit.kind === 'edit'}
                      placeholder="如 claude-opus"
                      className="w-full px-3 py-2 rounded-md bg-input-background border border-border text-sm outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
                    />
                  </FormField>
                  <FormField label="Provider" required>
                    <select
                      value={form.provider}
                      onChange={e => setForm({ ...form, provider: e.target.value })}
                      className="w-full px-3 py-2 rounded-md bg-input-background border border-border text-sm outline-none focus:ring-2 focus:ring-ring"
                    >
                      {providers.map(prov => (
                        <option key={prov} value={prov}>{prov}</option>
                      ))}
                    </select>
                  </FormField>
                  <FormField label="Model" required>
                    <input
                      type="text"
                      value={form.model}
                      onChange={e => setForm({ ...form, model: e.target.value })}
                      placeholder="如 claude-opus-4-7"
                      className="w-full px-3 py-2 rounded-md bg-input-background border border-border text-sm outline-none focus:ring-2 focus:ring-ring"
                    />
                  </FormField>
                  <FormField label="Base URL（relay 必填）">
                    <input
                      type="text"
                      value={form.base_url}
                      onChange={e => setForm({ ...form, base_url: e.target.value })}
                      placeholder="https://api.example.com/v1"
                      className="w-full px-3 py-2 rounded-md bg-input-background border border-border text-sm outline-none focus:ring-2 focus:ring-ring"
                    />
                  </FormField>
                  <FormField label="API Key / Token" full>
                    <input
                      type="text"
                      value={form.api_key}
                      onChange={e => setForm({ ...form, api_key: e.target.value })}
                      placeholder="sk-..."
                      className="w-full px-3 py-2 rounded-md bg-input-background border border-border text-sm font-mono outline-none focus:ring-2 focus:ring-ring"
                    />
                  </FormField>
                  <FormField label="备注" full>
                    <textarea
                      value={form.notes}
                      onChange={e => setForm({ ...form, notes: e.target.value })}
                      rows={2}
                      placeholder="任意文字 · 比如来源、用途、限额提醒"
                      className="w-full px-3 py-2 rounded-md bg-input-background border border-border text-sm outline-none focus:ring-2 focus:ring-ring resize-none"
                    />
                  </FormField>
                </div>
                <div className="flex justify-end gap-2 mt-5">
                  <button
                    onClick={cancel}
                    disabled={pending}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-border text-sm hover:bg-muted transition-colors disabled:opacity-50"
                  >
                    <X className="w-4 h-4" />
                    取消
                  </button>
                  <button
                    onClick={save}
                    disabled={pending}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-foreground text-background text-sm hover:opacity-90 transition-opacity disabled:opacity-50"
                  >
                    <Check className="w-4 h-4" />
                    {pending ? '保存中…' : '保存'}
                  </button>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* preset 列表 */}
        <div className="space-y-3">
          {data.presets.length === 0 && edit.kind === 'none' && (
            <div className="text-center py-12 text-sm text-muted-foreground">
              还没有 preset，点击右上角「添加 preset」开始
            </div>
          )}
          {data.presets.map(p => {
            const isActive = p.name === data.active;
            const isFallback = p.name === data.fallback;
            const isRevealed = revealed.has(p.name);
            return (
              <motion.div
                key={p.name}
                layout
                className="bg-card border border-border rounded-lg p-4"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="text-foreground" style={{ fontSize: '0.95rem', fontWeight: 500 }}>
                        {p.name}
                      </h3>
                      {isActive && <Tag color="primary">主</Tag>}
                      {isFallback && <Tag color="muted">备</Tag>}
                    </div>
                    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm">
                      <Row icon={<Cpu className="w-3.5 h-3.5" />} label="Provider">
                        <span className="text-foreground">{p.provider}</span>
                        <span className="mx-2 text-muted-foreground">·</span>
                        <span className="font-mono text-foreground">{p.model || <em className="text-muted-foreground not-italic">未设置</em>}</span>
                      </Row>
                      <Row icon={<Server className="w-3.5 h-3.5" />} label="Base URL">
                        <span className="font-mono text-xs text-foreground break-all">
                          {p.base_url || <em className="text-muted-foreground not-italic">默认</em>}
                        </span>
                      </Row>
                      <Row icon={<KeyRound className="w-3.5 h-3.5" />} label="Token">
                        <span className="font-mono text-xs text-foreground break-all">
                          {isRevealed ? (p.api_key || '(未设置)') : maskKey(p.api_key)}
                        </span>
                        {p.api_key && (
                          <button
                            onClick={() => toggleReveal(p.name)}
                            className="ml-2 text-muted-foreground hover:text-foreground"
                            title={isRevealed ? '隐藏' : '显示'}
                          >
                            {isRevealed ? <EyeOff className="w-3.5 h-3.5 inline" /> : <Eye className="w-3.5 h-3.5 inline" />}
                          </button>
                        )}
                      </Row>
                      {p.notes && (
                        <Row icon={<FileText className="w-3.5 h-3.5" />} label="备注">
                          <span className="text-foreground whitespace-pre-wrap">{p.notes}</span>
                        </Row>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-col gap-1.5 flex-shrink-0">
                    <button
                      onClick={() => setActive(p.name)}
                      disabled={isActive}
                      className="px-2.5 py-1 rounded text-xs border border-border hover:bg-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                      title={isActive ? '当前主 preset' : '设为主 preset'}
                    >
                      设为主
                    </button>
                    <button
                      onClick={() => isFallback ? setFallback(null) : setFallback(p.name)}
                      disabled={isActive}
                      className="px-2.5 py-1 rounded text-xs border border-border hover:bg-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                      title={isFallback ? '取消 fallback' : '设为 fallback'}
                    >
                      {isFallback ? '取消备' : '设为备'}
                    </button>
                    <button
                      onClick={() => startEdit(p)}
                      className="flex items-center justify-center gap-1 px-2.5 py-1 rounded text-xs border border-border hover:bg-muted transition-colors"
                    >
                      <Pencil className="w-3 h-3" />
                      编辑
                    </button>
                    <button
                      onClick={() => remove(p.name)}
                      disabled={isActive || isFallback}
                      className="flex items-center justify-center gap-1 px-2.5 py-1 rounded text-xs border border-border hover:bg-destructive/10 hover:border-destructive/40 hover:text-destructive transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                      title={isActive || isFallback ? '请先取消主/备身份再删除' : '删除'}
                    >
                      <Trash2 className="w-3 h-3" />
                      删除
                    </button>
                  </div>
                </div>
              </motion.div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── 子组件 ───────────────────────────────────────────────────────

function FormField({
  label,
  required,
  disabled,
  full,
  children,
}: {
  label: string;
  required?: boolean;
  disabled?: boolean;
  full?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={full ? 'col-span-2' : ''}>
      <label className={`block text-xs mb-1.5 ${disabled ? 'text-muted-foreground' : 'text-foreground'}`} style={{ fontWeight: 500 }}>
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </label>
      {children}
    </div>
  );
}

function Row({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <span className="text-muted-foreground mt-0.5 flex-shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <span className="text-xs text-muted-foreground mr-2">{label}</span>
        {children}
      </div>
    </div>
  );
}

function Tag({ children, color }: { children: React.ReactNode; color: 'primary' | 'muted' }) {
  const cls =
    color === 'primary'
      ? 'bg-foreground text-background'
      : 'bg-muted text-muted-foreground';
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs ${cls}`} style={{ fontWeight: 500 }}>
      {children}
    </span>
  );
}
