import { useCallback, useEffect, useState } from "react";
import { driverRequest as request } from "./driverApi";
import { GpioBiasSection } from "./GpioBiasSection";
import { KernelPackagesPage } from "./KernelPackagesPage";

type Sample = {ok: boolean; temperature?: number; humidity?: number; error?: string; time: number};
type Driver = {id: string; title: string; module: string; kernel: string; real: boolean; installed: boolean; uploaded: boolean;
  loaded: boolean; active: boolean; device: string | null; config: {pin: string; enabled: boolean}; pins: string[]; reason: string; sample: Sample | null};
type Plan = {pin: string; enabled: boolean; hash: string; source: string; diff: string; ready: boolean; compiled: boolean; blockers: string[]; needs_reboot: boolean};
export function KernelDriversPage({onError}: {onError: (m: string) => void}) {
  const [driver, setDriver] = useState<Driver>();
  const [pin, setPin] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [backup, setBackup] = useState("");
  const [sample, setSample] = useState<Sample | null>(null);
  const reload = useCallback(async () => { const d: Driver = (await request("/drivers")).drivers[0]; setDriver(d); setSample(d.sample); return d; }, []);
  useEffect(() => { reload().then(d => {setPin(d.config.pin || "J10-16"); setEnabled(d.config.enabled || !d.config.pin);}, (e: Error) => onError(e.message)); }, [reload, onError]);
  const act = async (fn: () => Promise<void>) => {
    setBusy(true); try { await fn(); } catch (e) { onError((e as Error).message); } finally { setBusy(false); }
  };
  const clear = () => {setPlan(null); setBackup("");};
  if (!driver) return <p className="text-muted">Загрузка драйверов…</p>;
  return <section>
    <GpioBiasSection onError={onError} />
    <KernelPackagesPage onError={onError} />
    <h2 className="mb-2 text-lg font-semibold">Готовый профиль подключения</h2>
    <p className="mb-5 text-muted">Настройка DATA-пина и чтение DHT22 / AM2302 через установленный драйвер Linux.</p>
    <div className="rounded border border-line p-4">
      <h3 className="mb-2 font-semibold">{driver.title}</h3>
      <p className="text-muted">Драйвер <code>dht11</code> · IIO · интервал чтения не менее 2 с</p>
      <p className="mt-2 break-all font-mono text-xs">Ядро: {driver.kernel}</p>
      <div className="my-3 flex flex-wrap gap-3 text-xs">
        <span>Модуль: {driver.installed ? "установлен" : "отсутствует"}</span>
        <span>В ядре: {driver.loaded ? "загружен" : "не загружен"}</span>
        <span>Датчик: {driver.active ? driver.device : "не зарегистрирован в IIO"}</span>
      </div>
      {driver.reason && <p className="mb-3 text-warn">{driver.reason}</p>}
      {!driver.real && <p className="mb-3 text-warn">На виртуальном стенде установка, загрузка и изменение boot недоступны.</p>}
      <div className="mb-5 flex gap-3 text-xs">
        <button disabled={busy || !driver.real || !driver.installed || driver.loaded} onClick={() => {
          if (confirm("Загрузить драйвер dht11 в работающее ядро? Если датчик уже описан в Device Tree, драйвер получит его DATA-пин.")) act(async () => {await request("/drivers/dht22/load?confirmed=true", {}); await reload();});
        }} className="rounded bg-panel-2 px-3 py-2 disabled:opacity-40">Загрузить в ядро</button>
      </div>
      <p className="mb-3 text-muted">Подключение модуля: DAT → выбранный GPIO, VCC → J9-1 (+3V3), GND → J10-1. Красная плата не гарантирует расположение контактов — ориентируйтесь по надписям. DATA должен быть подтянут к 3.3 В; при отсутствии резистора на модуле добавьте 4.7–10 кОм.</p>
      <div className="mb-3 flex items-center gap-3">
        <label>DATA <select aria-label="Пин DATA датчика" value={pin} disabled={busy} onChange={e => {setPin(e.target.value); clear();}} className="ml-2 rounded border border-line bg-bg px-2 py-1">
          {driver.pins.map(p => <option key={p} value={p}>{p}</option>)}
        </select></label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={enabled} disabled={busy} onChange={e => {setEnabled(e.target.checked); clear();}} />Подключать после загрузки</label>
      </div>
      <button disabled={busy} onClick={() => act(async () => {setPlan(await request("/drivers/dht22/plan", {pin, enabled})); setBackup("");})} className="rounded bg-panel-2 px-3 py-2 text-xs disabled:opacity-40">Предпросмотр подключения</button>
      {plan && <div className="mt-3 rounded border border-line bg-bg p-3 text-xs">
        {plan.blockers.map((b,i) => <p className="mb-2 text-warn" key={i}>{b}</p>)}
        {plan.compiled && <p className="mb-2 text-ok">Оверлей скомпилирован; цепочка оверлеев проверена на DTB этой платы.</p>}
        <details><summary className="cursor-pointer">Device Tree</summary><pre className="overflow-auto whitespace-pre-wrap py-2">{plan.source || "Отключение датчика"}</pre></details>
        <pre className="my-2 overflow-auto whitespace-pre-wrap">{plan.diff || "uEnv уже содержит эту настройку"}</pre>
        <p className="text-warn">Применение меняет uEnv.txt. Потребуется перезагрузка; до неё пины и датчик работают по прежней конфигурации.</p>
        {!backup && <button disabled={busy || !plan.ready || !driver.real} onClick={() => {
          if (confirm(`Применить подключение DHT22 на ${plan.pin}? Будет сохранён бэкап uEnv.txt. Ошибка загрузочной конфигурации может потребовать восстановления через UART или microSD. Перезагрузка выполняется отдельно.`)) act(async () => {
            const r = await request("/drivers/dht22/apply", {...plan, confirmed: true}); setBackup(r.backup); await reload();
          });
        }} className="mt-3 rounded bg-accent px-3 py-2 text-white disabled:opacity-40">Применить после подтверждения</button>}
        {backup && <><p className="mt-3 break-all text-ok">Сохранено. Бэкап: {backup}</p>
          <button disabled={busy} className="mt-3 rounded bg-err px-3 py-2 text-white" onClick={() => {
            if (confirm("Перезагрузить плату сейчас? Все проекты и скрипты будут остановлены.")) act(async () => {await request("/board/reboot", {confirmation: "REBOOT"});});
          }}>Перезагрузить плату</button></>}
      </div>}
      <div className="mt-5 border-t border-line pt-3">
        <button disabled={busy || !driver.active} onClick={() => act(async () => {setSample(await request("/drivers/dht22/read", {}));})} className="rounded bg-panel-2 px-3 py-2 text-xs disabled:opacity-40">Прочитать датчик</button>
        <button disabled={busy} onClick={() => act(async () => {await reload();})} className="ml-3 text-xs text-muted">Обновить статус</button>
        {sample && <p className={`mt-3 ${sample.ok ? "text-ok" : "text-warn"}`}>{sample.ok ? `${sample.temperature} °C · ${sample.humidity} % RH` : sample.error}</p>}
      </div>
    </div>
    <p className="mt-4 text-xs text-muted">Нужен модуль из сборки именно текущего ядра. <a href="#/help/drivers" className="text-accent">Инструкция подключения и сборки</a>.</p>
  </section>;
}
