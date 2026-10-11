import { useCallback, useEffect, useState } from "react";
import { driverRequest as request } from "./driverApi";

type Module = {name: string; description: string; license: string; vermagic: string; depends: string; sha256: string; loaded: boolean};
type Package = {id: string; title: string; modules: Module[]; loaded: boolean; autoload: boolean; overlay: string | null; pins: string[]; parameters: Record<string, string[]>};
type Catalog = {kernel: string; real: boolean; packages: Package[]};
type Plan = {id: string; enabled: boolean; hash: string; ready: boolean; compiled: boolean; blockers: string[]; source: string; diff: string};

export function KernelPackagesPage({onError}: {onError: (m: string) => void}) {
  const [catalog, setCatalog] = useState<Catalog>();
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [backup, setBackup] = useState("");
  const refresh = useCallback(async () => setCatalog(await request("/drivers/packages")), []);
  useEffect(() => {refresh().catch((e: Error) => onError(e.message));}, [refresh, onError]);
  const act = async (fn: () => Promise<void>) => {
    setBusy(true); try {await fn();} catch (e) {onError((e as Error).message);} finally {setBusy(false);}
  };
  return <section className="mb-8">
    <h2 className="mb-2 text-lg font-semibold">Пакеты драйверов</h2>
    <p className="mb-3 text-muted">Загрузите доверенный .ko или ZIP с manifest.json, модулями и необязательным DTBO. Новые пакеты подключаются без изменения Pioneer. Модули должны соответствовать текущему ядру {catalog?.kernel}.</p>
    <div className="mb-4 flex items-center gap-4 text-xs">
      <label className={`rounded bg-accent px-3 py-2 text-white ${busy || !catalog?.real ? "opacity-40" : "cursor-pointer"}`}>Загрузить .ko / .zip
        <input type="file" accept=".ko,.zip" className="hidden" disabled={busy || !catalog?.real} onChange={e => {
          const f = e.target.files?.[0]; e.target.value = "";
          if (!f || !confirm("Установить доверенный пакет драйвера? Код модулей исполняется с правами ядра и может нарушить работу платы. Проверка архитектуры/vermagic не подтверждает происхождение файла. Установка сама не загружает модули и не меняет boot.")) return;
          act(async () => {await request("/drivers/packages?confirmed=true", undefined, f); setPlan(null); await refresh();});
        }} />
      </label>
      <button disabled={busy} className="text-muted disabled:opacity-40" onClick={() => act(refresh)}>Обновить</button>
      <a href="#/help/drivers" className="text-accent">Формат пакета и ограничения</a>
    </div>
    {!catalog?.real && <p className="mb-3 text-xs text-warn">На стенде установка и загрузка модулей недоступны.</p>}
    {catalog?.packages.length === 0 && <p className="mb-3 text-xs text-muted">Универсальные пакеты пока не установлены. DHT22 можно настроить готовым профилем ниже.</p>}
    {catalog?.packages.map(p => <div key={p.id} className="mb-3 rounded border border-line p-4">
      <h3 className="font-semibold">{p.title} <span className="ml-2 text-xs text-muted">{p.id}</span></h3>
      <ul className="my-3 space-y-2 text-xs">{p.modules.map(m => <li key={m.name}>
        <code>{m.name}</code> · {m.loaded ? "загружен" : "не загружен"} · {m.license || "лицензия не указана"}
        <details className="mt-1 text-muted"><summary className="cursor-pointer">Метаданные</summary>
          <p>{m.description}</p><p className="break-all">vermagic: {m.vermagic}</p>
          <p>Зависимости: {m.depends || "нет"}</p><p className="break-all">SHA-256: {m.sha256}</p>
          <p>Параметры: {p.parameters[m.name]?.join(" ") || "по умолчанию"}</p>
        </details>
      </li>)}</ul>
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <button disabled={busy || p.loaded || !catalog.real} className="rounded bg-panel-2 px-3 py-2 disabled:opacity-40" onClick={() => {
          if (confirm(`Загрузить модули пакета ${p.title} в ядро? Они могут получить управление описанными устройствами.`)) act(async () => {await request(`/drivers/packages/${p.id}/load?confirmed=true`, {}); await refresh();});
        }}>Загрузить</button>
        <button disabled={busy || p.autoload || !catalog.real || !p.modules.some(m => m.loaded)} className="rounded bg-panel-2 px-3 py-2 disabled:opacity-40" onClick={() => {
          if (confirm(`Потребители ${p.title} остановлены? Выгрузить модули без force? Устройства пакета станут недоступны.`)) act(async () => {await request(`/drivers/packages/${p.id}/unload?confirmed=true`, {}); await refresh();});
        }}>Выгрузить</button>
        <label className="flex items-center gap-2"><input type="checkbox" checked={p.autoload} disabled={busy || !catalog.real} onChange={e => {
          const enabled = e.target.checked;
          if (confirm(`${enabled ? "Включить" : "Отключить"} загрузку пакета ${p.title} при старте Pioneer?`)) act(async () => {await request(`/drivers/packages/${p.id}/autoload`, {enabled, confirmed: true}); await refresh();});
        }} />Автозагрузка</label>
        {p.overlay && <><button disabled={busy} className="text-accent" onClick={() => act(async () => {setPlan(await request(`/drivers/packages/${p.id}/plan`, {enabled: true})); setBackup("");})}>Предпросмотр подключения</button>
          <button disabled={busy} className="text-muted" onClick={() => act(async () => {setPlan(await request(`/drivers/packages/${p.id}/plan`, {enabled: false})); setBackup("");})}>Предпросмотр отключения</button></>}
      </div>
      {p.overlay && <p className="mt-3 text-xs text-muted">DTBO: {p.overlay} · эксклюзивные GPIO: {p.pins.join(", ") || "нет"}. Пакет должен описывать подключение именно этой платы.</p>}
    </div>)}
    {plan && <div className="rounded border border-line bg-bg p-4 text-xs">
      <h3 className="mb-2 font-semibold">{plan.enabled ? "Подключение" : "Отключение"} {plan.id}</h3>
      {plan.blockers.map((b,i) => <p key={i} className="text-warn">{b}</p>)}
      {plan.compiled && <p className="text-ok">Цепочка оверлеев проверена на DTB платы.</p>}
      <details><summary className="cursor-pointer">Содержимое оверлея</summary><pre className="overflow-auto whitespace-pre-wrap">{plan.source}</pre></details>
      <pre className="my-3 overflow-auto whitespace-pre-wrap">{plan.diff || "uEnv уже содержит настройку"}</pre>
      <p className="text-warn">Изменение boot требует перезагрузки. Ошибка оверлея может потребовать UART/microSD для восстановления.</p>
      {!backup && <button disabled={busy || !plan.ready || !catalog?.real} className="mt-3 rounded bg-accent px-3 py-2 text-white disabled:opacity-40" onClick={() => {
        if (confirm("Применить показанное изменение uEnv? Будет сохранён бэкап. Перезагрузка — отдельным действием.")) act(async () => {const r = await request(`/drivers/packages/${plan.id}/apply`, {...plan, confirmed: true}); setBackup(r.backup);});
      }}>Применить</button>}
      {backup && <><p className="mt-3 break-all text-ok">Сохранено. Бэкап: {backup}</p><button disabled={busy} className="mt-3 rounded bg-err px-3 py-2 text-white" onClick={() => {
        if (confirm("Перезагрузить плату? Все проекты и скрипты будут остановлены.")) act(async () => {await request("/board/reboot", {confirmation: "REBOOT"});});
      }}>Перезагрузить плату</button></>}
    </div>}
  </section>;
}
