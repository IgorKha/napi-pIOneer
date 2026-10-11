import {useEffect, useState} from 'react';
type Status = {available: boolean; armed: boolean; timeout: number; identity: string | null; nowayout: string | null; error: string};
export function WatchdogSection() {
  const [status, setStatus] = useState<Status>(); const [error, setError] = useState('');
  const [seconds, setSeconds] = useState(60); const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  async function request(path='', body?: object) {
    const r = await fetch('/pioneer/api/watchdog'+path, {method:body?'POST':'GET', headers:{'X-Pioneer':'1','Content-Type':'application/json'}, body:body?JSON.stringify(body):undefined});
    const data = await r.json(); if (!r.ok) throw new Error(data.detail ?? 'Watchdog недоступен'); setStatus(data);
  }
  useEffect(()=>{request().catch(e=>setError(e.message));},[]);
  async function apply(path:string) {setBusy(true);setError('');try{await request(path,{timeout:seconds,confirmed});}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section className="mt-6 rounded border border-line p-4 text-xs">
    <h3 className="mb-2 font-semibold">Аппаратный watchdog</h3>
    <p className="mb-2 text-muted">При зависании event loop или аварийном завершении сервиса watchdog перезагрузит плату. Штатная остановка отключает его. После запуска сервиса включается вручную; автозапуска нет.</p>
    {status && <p className="mb-3">{status.available ? `${status.identity ?? 'watchdog0'} · ${status.armed?'включён':'выключен'} · ${status.timeout} с` : 'Устройство watchdog отсутствует'}</p>}
    <label className="block mb-2">Таймаут, секунды <input aria-label="Таймаут watchdog" type="number" min={30} max={180} value={seconds} onChange={e=>setSeconds(Number(e.target.value))} className="ml-2 w-20 rounded bg-bg p-1"/></label>
    <label className="flex gap-2 mb-3"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>Разрешаю аппаратную перезагрузку при отказе сервиса</label>
    <button disabled={busy || !status?.available || status.armed || !confirmed || status.nowayout!=='0'} onClick={()=>apply('/arm')} className="rounded bg-warn px-3 py-2 text-bg disabled:opacity-40">Включить watchdog</button>
    <button disabled={busy || !status?.armed} onClick={()=>apply('/disarm')} className="ml-3 text-muted disabled:opacity-40">Отключить</button>
    {(error || status?.error) && <p role="alert" className="mt-3 text-err">{error || status?.error}</p>}
  </section>;
}
