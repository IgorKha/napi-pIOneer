import {WatchdogSection} from "./WatchdogSection";
import { LimitsSection } from "./LimitsSection";
import { useEffect, useState } from "react";

interface Status {
  version: string; hal: string; kernel: string; uptime_seconds: number;
  memory: {core_rss_bytes: number | null; available_bytes: number | null; total_bytes: number | null};
  disk: {free_bytes: number; total_bytes: number}; projects: {total: number; running: number}; scripts: {total: number; running: number};
  claims: number; mqtt_connected: boolean; ai_busy: boolean; drivers: {id: string; loaded: boolean; autoload: boolean}[]; warnings: string[];
}
const mb = (n: number | null) => n === null ? "недоступно" : `${(n / 1024**2).toFixed(1)} МиБ`;

export function SystemPage() {
  const [status, setStatus] = useState<Status>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true); setError("");
    try {
      const r = await fetch("/pioneer/api/system");
      if (!r.ok) throw new Error(`Диагностика: HTTP ${r.status}`);
      setStatus(await r.json());
    } catch (e) {setError((e as Error).message);} finally {setBusy(false);}
  };
  useEffect(() => {load();}, []);
  return <section>
    <h2 className="mb-2 text-lg font-semibold">Система</h2>
    <p className="mb-4 text-muted">Текущее состояние сервиса и ресурсы платы. Отчёт содержит показатели и статусы; ключи, код и значения секретов в него не входят.</p>
    <div className="mb-5 flex gap-4 text-xs"><button disabled={busy} onClick={load} className="rounded bg-panel-2 px-3 py-2 disabled:opacity-40">{busy ? "Обновляю…" : "Обновить"}</button><a className="py-2 text-accent" href="/pioneer/api/system/report" download>Скачать отчёт JSON</a><a className="py-2 text-accent" href="#/help/system">Сохранность и диагностика</a></div>
    {error && <p role="alert" className="mb-3 text-err">{error}</p>}
    {status && <>
      {status.warnings.map(w => <p key={w} role="alert" className="mb-3 rounded border border-warn/40 p-3 text-warn">{w}</p>)}
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 rounded border border-line p-4 text-xs">
        <dt className="text-muted">Сервис</dt><dd>pioneer {status.version} · {status.hal}</dd>
        <dt className="text-muted">Ядро</dt><dd className="break-all font-mono">{status.kernel}</dd>
        <dt className="text-muted">После запуска</dt><dd>{Math.floor(status.uptime_seconds / 60)} мин {status.uptime_seconds % 60} с</dd>
        <dt className="text-muted">Память core</dt><dd>{mb(status.memory.core_rss_bytes)}</dd>
        <dt className="text-muted">Доступно RAM</dt><dd>{mb(status.memory.available_bytes)} / {mb(status.memory.total_bytes)}</dd>
        <dt className="text-muted">Свободно на диске</dt><dd>{mb(status.disk.free_bytes)} / {mb(status.disk.total_bytes)}</dd>
        <dt className="text-muted">Проекты</dt><dd>{status.projects.running} работают / {status.projects.total} всего</dd>
        <dt className="text-muted">Скрипты</dt><dd>{status.scripts.running} работают / {status.scripts.total} всего</dd>
        <dt className="text-muted">Ресурсы</dt><dd>{status.claims} захватов</dd>
        <dt className="text-muted">MQTT</dt><dd>{status.mqtt_connected ? "Подключён" : "Нет подключения"}</dd>
        <dt className="text-muted">AI</dt><dd>{status.ai_busy ? "Отвечает" : "Готов к запросу"}</dd>
      </dl>
      <h3 className="mt-5 mb-2 font-semibold">Пакеты драйверов</h3>
      <ul className="space-y-1 text-xs text-muted">{status.drivers.map(d => <li key={d.id}>{d.id} · {d.loaded ? "загружен" : "не загружен"} · автозагрузка {d.autoload ? "включена" : "выключена"}</li>)}</ul>
      {!status.drivers.length && <p className="text-xs text-muted">Нет установленных пакетов.</p>}
    </>}
    <LimitsSection />
    <WatchdogSection />
  </section>;
}
