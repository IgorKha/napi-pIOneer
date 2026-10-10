import { useCallback, useEffect, useState } from "react";
import { api, type MqttSettings, type MqttState } from "./api";

/** Настройки: MQTT (idea 003) и секреты (spec 6): значения только записываются; в проектах — ссылки {{secret:имя}}. */
export function SecretsPage({ onError }: { onError: (msg: string) => void }) {
  const [names, setNames] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const reload = useCallback(() => api.secrets().then((r) => setNames(r.names), (e: Error) => onError(e.message)), [onError]);
  useEffect(() => { reload(); }, [reload]);

  const put = (patch: Record<string, string | null>) =>
    api.secretsUpdate(patch).then((r) => setNames(r.names), (e: Error) => onError(e.message));

  return (
    <div className="mx-auto w-full max-w-xl overflow-auto p-8 text-sm">
      <MqttSection onError={onError} secretNames={names} />
      <h2 className="mt-10 mb-1 text-base font-semibold">Секреты</h2>
      <p className="mb-5 text-muted">
        Токены и пароли для нод «HTTP-запрос», «Входящий webhook» и т.п. Хранятся только на плате (файл с правами 600),
        в браузер не возвращаются. В параметрах нод пишите ссылку <code className="font-mono text-fg">{"{{secret:имя}}"}</code>.
      </p>
      <form className="mb-6 flex gap-2" onSubmit={(e) => {
        e.preventDefault();
        if (!name.trim() || !value) return;
        put({ [name.trim()]: value }).then(() => { setName(""); setValue(""); });
      }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="имя (латиница, _ . -)" aria-label="Имя секрета"
               className="w-48 rounded border border-line bg-bg px-2 py-1.5 font-mono outline-none focus:border-accent" />
        <input value={value} onChange={(e) => setValue(e.target.value)} type="password" placeholder="значение" aria-label="Значение секрета"
               autoComplete="new-password"
               className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-1.5 outline-none focus:border-accent" />
        <button className="rounded bg-accent px-3 py-1.5 text-white disabled:opacity-40" disabled={!name.trim() || !value}>
          Сохранить
        </button>
      </form>
      <ul className="divide-y divide-line rounded border border-line">
        {names.map((n) => (
          <li key={n} className="flex items-center gap-3 px-3 py-2">
            <code className="font-mono">{`{{secret:${n}}}`}</code>
            <span className="text-muted">••••••</span>
            <button onClick={() => navigator.clipboard?.writeText(`{{secret:${n}}}`)} className="ml-auto text-xs text-muted hover:text-fg">
              копировать ссылку
            </button>
            <button onClick={() => confirm(`Удалить секрет «${n}»?`) && put({ [n]: null })} className="text-xs text-muted hover:text-err">
              удалить
            </button>
          </li>
        ))}
        {names.length === 0 && <li className="px-3 py-2 text-muted">Пока нет секретов</li>}
      </ul>
      <p className="mt-3 text-xs text-muted">Чтобы сменить значение — сохраните секрет с тем же именем.</p>
    </div>
  );
}

function MqttSection({ onError, secretNames }: { onError: (m: string) => void; secretNames: string[] }) {
  const [st, setSt] = useState<MqttState | null>(null);
  const [form, setForm] = useState<MqttSettings | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const load = () => api.mqtt().then((r) => { setSt(r); setForm((f) => f ?? r.settings); }, () => {});
    load();
    const t = window.setInterval(() => api.mqtt().then(setSt, () => {}), 3000); // статус подключения
    return () => window.clearInterval(t);
  }, []);
  if (!st || !form) return <p className="text-muted">Загрузка…</p>;
  const set = <K extends keyof MqttSettings>(k: K, v: MqttSettings[K]) => setForm({ ...form, [k]: v });
  const save = () => {
    setBusy(true);
    api.mqttSave(form).then((r) => { setSt(r); setForm(r.settings); }, (e: Error) => onError(e.message)).finally(() => setBusy(false));
  };
  const s = st.status;
  const field = "rounded border border-line bg-bg px-2 py-1.5 outline-none focus:border-accent";
  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">MQTT</h1>
      <p className="mb-4 text-muted">
        Удалённые команды, состояния и события через брокер (ноды «Команда», «Состояние», «Публикация», «Связь»).
        Топики: <code className="font-mono text-fg">{s.prefix}/cmd/&lt;команда&gt;</code>, <code className="font-mono text-fg">…/state/…</code>,{" "}
        <code className="font-mono text-fg">…/status</code>.
      </p>
      <div className="mb-3 flex items-center gap-2">
        <span className={`size-2 rounded-full ${!s.enabled ? "bg-muted" : s.connected ? "bg-ok" : "bg-err"}`} />
        <span>{!s.enabled ? "выключено" : s.connected ? `подключено к ${s.broker}` : `нет связи с ${s.broker}`}</span>
        {s.error && <span className="text-err">— {s.error}</span>}
      </div>
      <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2">
        <span className="text-muted">Включено</span>
        <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} className="justify-self-start accent-accent" />
        <span className="text-muted">Брокер</span>
        <div className="flex gap-2">
          <input value={form.host} onChange={(e) => set("host", e.target.value)} className={`${field} min-w-0 flex-1`} aria-label="Адрес брокера" />
          <input value={form.port} onChange={(e) => set("port", Number(e.target.value) || 0)} className={`${field} w-20 text-right`} aria-label="Порт" />
        </div>
        <span className="text-muted">Логин</span>
        <input value={form.username} onChange={(e) => set("username", e.target.value)} className={field} aria-label="Логин" autoComplete="off" />
        <span className="text-muted">Пароль</span>
        <select value={form.password} onChange={(e) => set("password", e.target.value)} className={field} aria-label="Пароль (секрет)">
          <option value="">— без пароля —</option>
          {secretNames.map((n) => <option key={n} value={`{{secret:${n}}}`}>{`{{secret:${n}}}`}</option>)}
        </select>
        <span className="text-muted">TLS</span>
        <input type="checkbox" checked={form.tls} onChange={(e) => set("tls", e.target.checked)} className="justify-self-start accent-accent" />
        <span className="text-muted">Id платы</span>
        <input value={form.device_id} onChange={(e) => set("device_id", e.target.value)} className={`${field} font-mono`} aria-label="Id платы" />
      </div>
      <button onClick={save} disabled={busy} className="mt-4 rounded bg-accent px-3 py-1.5 text-white disabled:opacity-40">
        Сохранить и переподключить
      </button>
    </section>
  );
}
