import { useEffect, useState } from "react";

interface Field { name: string; min: number; max: number; title: string; doc: string }
interface Limits { values: Record<string, number | boolean>; defaults: Record<string, number | boolean>; fields: Field[] }

async function request(body?: unknown): Promise<Limits> {
  const r = await fetch("/pioneer/api/settings/limits", {
    method: body === undefined ? "GET" : "PUT",
    headers: { "X-Pioneer": "1", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Таймауты: HTTP ${r.status}`);
  return data;
}

/** Настройки → Система: таймауты процессов блоков и скриптов (pioneer.limits). */
export function LimitsSection() {
  const [limits, setLimits] = useState<Limits>();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  const accept = (l: Limits) => {
    setLimits(l);
    setDraft(Object.fromEntries(l.fields.map(f => [f.name, String(l.values[f.name])])));
  };
  useEffect(() => { request().then(accept, e => setError((e as Error).message)); }, []);

  const save = async (body: Record<string, number | boolean | null>) => {
    setBusy(true); setError(""); setSaved(false);
    try { accept(await request(body)); setSaved(true); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const changed = limits ? limits.fields.filter(f => Number(draft[f.name]) !== limits.values[f.name]) : [];
  const submit = () => {
    const body: Record<string, number> = {};
    for (const f of changed) {
      const n = Number(draft[f.name].replace(",", "."));
      if (!Number.isFinite(n)) return setError(`${f.title}: нужно число`);
      body[f.name] = n;
    }
    save(body);
  };
  const resetAll = () => limits && save(Object.fromEntries(Object.keys(limits.values).map(k => [k, null])));

  return <section className="mt-6">
    <h3 className="mb-1 font-semibold">Таймауты</h3>
    <p className="mb-3 text-xs text-muted">
      Сколько ждать процессы Python-блоков и скриптов. Медленный <code>setup()</code> (например, инициализация LCD по I2C)
      упирается в «Загрузку блока», зависший обработчик — в «Обработчик блока». Новые значения действуют без перезапуска
      сервиса: вызовы — сразу, запуск процесса блоков — со следующего старта проекта.
    </p>
    {error && <p role="alert" className="mb-3 text-err">{error}</p>}
    {limits && <>
      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-2 rounded border border-line p-4 text-xs">
        {limits.fields.map(f => {
          const isDefault = limits.values[f.name] === limits.defaults[f.name];
          return <label key={f.name} className="contents">
            <span>
              <span className="font-medium">{f.title}</span>
              <span className="block text-muted">{f.doc} {f.min}…{f.max} с, по умолчанию {String(limits.defaults[f.name])} с.</span>
            </span>
            <span className="flex items-center gap-2 self-center">
              <input inputMode="decimal" value={draft[f.name] ?? ""} aria-label={f.title}
                     onChange={e => setDraft(d => ({ ...d, [f.name]: e.target.value }))}
                     className={`w-20 rounded border border-line bg-bg px-2 py-1 text-right font-mono ${isDefault ? "" : "text-accent"}`} />
              <span className="text-muted">с</span>
            </span>
          </label>;
        })}
        <label className="col-span-2 mt-1 flex items-center gap-2">
          <input type="checkbox" checked={limits.values.block_preload === true}
                 onChange={e => save({ block_preload: e.target.checked })} className="accent-accent" />
          <span>Предзагружать файлы блоков при запуске процесса <span className="text-muted">— импорт драйверов не съедает время «Загрузки блока»</span></span>
        </label>
        <label className="col-span-2 flex items-center gap-2">
          <input type="checkbox" checked={limits.values.direct_i2c === true}
                 onChange={e => save({ direct_i2c: e.target.checked })} className="accent-accent" />
          <span>Прямой I2C из блоков и скриптов <span className="text-muted">— адрес захватывается в core, транзакции идут без обхода через core (в разы быстрее для LCD и дисплеев). На стенде всегда через core.</span></span>
        </label>
      </div>
      <div className="mt-3 flex gap-4 text-xs">
        <button disabled={busy || !changed.length} onClick={submit} className="rounded bg-accent px-3 py-2 text-bg disabled:opacity-40">
          {busy ? "Сохраняю…" : "Сохранить"}
        </button>
        <button disabled={busy} onClick={resetAll} className="py-2 text-muted hover:text-fg">Вернуть умолчания</button>
        {saved && !changed.length && <span className="py-2 text-ok">Сохранено</span>}
      </div>
    </>}
  </section>;
}
