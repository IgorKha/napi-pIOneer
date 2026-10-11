import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { Op } from "../api";
import { Markdown } from "../help/Markdown";
import { ai, type AIArtifact, type AIMessage, type AIAttachment } from "./api";

const DraftEditor = lazy(() => import("./DraftEditor").then((m) => ({ default: m.DraftEditor })));

export function AIPanel({ id, rev, preview, onPreview, onAccept, onReference, onError, beforeSend, onFileSave }: {
  beforeSend?: () => Promise<void>; onFileSave?: (artifact: AIArtifact, code: string) => Promise<void>;
  id: string; rev: number; preview: AIArtifact | null; onPreview: (a: AIArtifact | null) => void;
  onAccept: (ops: Op[], rev: number) => Promise<boolean>; onReference: (url: string) => void; onError: (m: string) => void;
}) {
  const codeScope = id.startsWith("script:") || id.startsWith("block:");
  const label = id.startsWith("block:") ? "этот блок" : id.startsWith("script:") ? "этот скрипт" : "этот проект";
  const [messages, setMessages] = useState<AIMessage[]>([]);
  const [model, setModel] = useState("");
  const [attachments, setAttachments] = useState<AIAttachment[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [tool, setTool] = useState("");
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<AIArtifact | null>(null);
  const controller = useRef<AbortController | null>(null), tail = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let active = true;
    setLoading(true);
    ai.history(id).then((r) => { if (active) setMessages(r.messages); }, (e: Error) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.current?.abort(); };
  }, [id]);
  useEffect(() => { tail.current?.scrollIntoView({ block: "nearest" }); }, [messages, tool]);
  const decide = async (a: AIArtifact, status: string) => {
    try {
      await ai.decision(id, a.id, status);
      setMessages((ms) => ms.map((m) => ({ ...m, artifacts: m.artifacts?.map((x) => x.id === a.id ? { ...x, status } : x) })));
      if (preview?.id === a.id) onPreview(null);
    } catch (e) { setError((e as Error).message); }
  };
  const send = async () => {
    if (!text.trim() || busy || loading) return;
    const prompt = text.trim();
    const files = attachments; setAttachments([]); setText(""); setBusy(true); setError(""); setTool("");
    const abort = new AbortController(); controller.current = abort;
    let reply = "";
    setMessages((ms) => [...ms, { id: crypto.randomUUID(), role: "user", text: prompt, model, attachments:files.map(({name,type})=>({name,type})) }]);
    try {
      await beforeSend?.();
      await ai.stream(id, prompt, abort.signal, (e) => {
        if (e.type === "start") { reply = e.id!; setMessages((ms) => [...ms, { id: reply, role: "assistant", text: "", artifacts: [], citations: [] }]); }
        if (e.type === "tool") setTool(e.name ?? "");
        if (e.type === "error") setError(e.error ?? "Ошибка AI");
        if (e.type === "artifact" && e.artifact?.kind === "proposal") onPreview(e.artifact);
        setMessages((ms) => ms.map((m) => m.id !== reply ? m : e.type === "done" ? e.message! : {
          ...m, ...(e.type === "delta" ? { text: m.text + e.text } : {}),
          ...(e.type === "usage" ? { usage: e.usage } : {}),
          ...(e.type === "artifact" ? { artifacts: [...(m.artifacts ?? []), e.artifact!] } : {}),
          ...(e.type === "citation" ? { citations: [...(m.citations ?? []), { url: e.url!, title: e.title! }] } : {}),
        }));
      }, model.trim() || undefined, files);
    } catch (e) { if (!abort.signal.aborted) setError((e as Error).message); }
    finally { setBusy(false); setTool(""); controller.current = null; }
  };
  return <aside className="flex h-full w-96 shrink-0 flex-col bg-panel text-xs">
    <header className="flex items-center gap-2 border-b border-line p-3">
      <strong>AI · {label}</strong>
      <a href="#/secrets/ai" className="text-muted hover:text-accent">Настройки</a>
      <button disabled={busy || loading} onClick={async () => { try { await ai.reset(id); setMessages([]); onPreview(null); setError(""); } catch (e) { setError((e as Error).message); } }} className="ml-auto text-muted disabled:opacity-40">Новый чат</button>
    </header>
    <div className="min-h-0 flex-1 overflow-auto p-3" role="log" aria-live="polite">
      {messages.length === 0 && <p className="text-muted">{codeScope ? "Опишите задачу. Помощник прочитает файлы и предложит изменения кода для просмотра." : "Опишите задачу. Помощник проверит пины и схемы нод и предложит граф или Python-блок."}</p>}
      {messages.map((m) => <div key={m.id} className="mb-4 break-words">
        <div className="mb-1 font-semibold text-muted">{m.role === "user" ? "Вы" : "Помощник"}</div>
        {m.model && <p className="text-[10px] text-muted">{m.model}</p>}
        {m.attachments?.map((a,i)=><p key={i} className="text-accent">↳ {a.name}</p>)}
        <Markdown source={m.text} onReference={onReference} />
        {m.error && <p className="text-err">{m.error}</p>}
        {m.citations?.map((c, i) => /^https?:\/\//.test(c.url) && <a key={i} href={c.url} target="_blank" rel="noreferrer" className="mt-1 block truncate text-accent hover:underline">↗ {c.title}</a>)}
        {m.artifacts?.map((a) => <div key={a.id} className="mt-2 rounded border border-accent/50 bg-bg p-2">
          <p className="mb-2 font-semibold">{a.kind === "proposal" ? "Изменения графа" : a.kind === "file" ? `Файл: ${a.path}` : "Python-блок"}</p>
          <p>{a.summary}</p>
          {a.kind === "proposal" && <details className="my-2"><summary className="cursor-pointer text-muted">Операции ({a.ops?.length})</summary><pre className="max-h-48 overflow-auto whitespace-pre-wrap text-[10px]">{JSON.stringify(a.ops, null, 2)}</pre></details>}
          {a.validation?.incomplete.map((v, i) => <p className="text-warn" key={i}>{v.message}</p>)}
          {a.status !== "pending" ? <p className="mt-2 text-muted">{a.status === "accepted" ? "Принято" : "Отклонено"}</p> : <>
            {a.kind === "proposal" && a.rev !== rev && <p className="my-2 text-warn">Граф изменился. Попросите обновить предложение.</p>}
            <div className="mt-2 flex flex-wrap gap-2">
              {a.kind === "proposal" ? <>
                <button disabled={busy || a.rev !== rev} onClick={() => onPreview(preview?.id === a.id ? null : a)} className="rounded bg-panel-2 px-2 py-1 disabled:opacity-40">{preview?.id === a.id ? "Скрыть" : "Предпросмотр"}</button>
                <button disabled={busy || a.rev !== rev} onClick={async () => { if (await onAccept(a.ops!, a.rev!)) await decide(a, "accepted"); }} className="rounded bg-accent px-2 py-1 text-white disabled:opacity-40">Принять</button>
              </> : <button disabled={busy} onClick={() => setDraft(a)} className="rounded bg-accent px-2 py-1 text-white">Редактировать</button>}
              <button disabled={busy} onClick={() => decide(a, "rejected")} className="text-muted">Отклонить</button>
              <button onClick={() => setText(`Доработай предложение «${a.summary}»: `)} className="text-muted">Доработай…</button>
            </div>
          </>}
        </div>)}
        {m.usage && <p className="mt-2 text-[10px] text-muted">Токены: вход {m.usage.input ?? 0} · выход {m.usage.output ?? 0} · кэш {m.usage.cached ?? 0}</p>}
      </div>)}
      {busy && <p className="text-accent">{tool ? `Проверяю: ${tool}…` : "Отвечаю…"}</p>}
      <div ref={tail} />
    </div>
    {error && <p role="alert" className="border-t border-line p-3 text-err">{error}</p>}
    <form className="border-t border-line p-3" onSubmit={(e) => { e.preventDefault(); send(); }}>
      <label className="mb-2 block text-muted">Модель <input aria-label="Модель этого запроса" value={model} maxLength={120} onChange={e=>setModel(e.target.value)} placeholder="Из настроек AI" disabled={busy} className="ml-2 w-52 rounded border border-line bg-bg p-1 text-fg"/></label>
      <div className="mb-2 flex flex-wrap gap-2">{attachments.map((a,i)=><button type="button" key={i} disabled={busy} onClick={()=>setAttachments(files=>files.filter((_,j)=>i!==j))} className="rounded bg-panel-2 p-1" title="Убрать вложение">{a.name} ×</button>)}</div>
      <label className="mb-2 block cursor-pointer text-accent">Прикрепить файл<input type="file" multiple disabled={busy} accept=".txt,.md,.py,.json,.yaml,.yml,.toml,.csv,.png,.jpg,.jpeg,.webp" className="hidden" onChange={async e=>{
        const selected=Array.from(e.target.files??[]); e.target.value="";
        try {
          if(attachments.length+selected.length>4) throw new Error("До четырёх вложений");
          const result:AIAttachment[]=[];
          for(const f of selected){
            const image=["image/png","image/jpeg","image/webp"].includes(f.type);
            if(f.size>(image?256:64)*1024)throw new Error(`${f.name}: максимум ${image?256:64} КиБ`);
            if(image){
              const content=await new Promise<string>((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(",")[1]);r.onerror=()=>reject(new Error("Не удалось прочитать файл"));r.readAsDataURL(f);});
              result.push({name:f.name,type:f.type,content});
            }else result.push({name:f.name,type:"text",content:await f.text()});
          }
          setAttachments(files=>[...files,...result]);
        }catch(err){setError((err as Error).message);}
      }}/></label>
      {attachments.length>0 && <p className="mb-2 text-[10px] text-warn">При отправке эти файлы будут переданы настроенному AI-провайдеру. Изображения отправляются только с этим сообщением.</p>}
      <textarea aria-label="Сообщение AI" value={text} onChange={(e) => setText(e.target.value)} maxLength={8000} rows={3} className="w-full resize-none rounded border border-line bg-bg p-2 outline-none focus:border-accent" placeholder={codeScope ? "Например: добавь обработку MQTT-сообщений" : "Например: кнопка включает реле на 10 секунд"} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
      <div className="mt-2 flex justify-between"><span className="text-[10px] text-muted">Enter — отправить · Shift+Enter — строка</span>
        {busy ? <button type="button" onClick={() => controller.current?.abort()} className="text-err">Остановить</button> : <button disabled={!text.trim() || loading} className="rounded bg-accent px-3 py-1 text-white disabled:opacity-40">Отправить</button>}
      </div>
    </form>
    {draft && <Suspense fallback={null}><DraftEditor draft={draft} onFileSave={onFileSave ? code => onFileSave(draft, code) : undefined} onClose={() => setDraft(null)} onSaved={() => { decide(draft, "accepted"); setDraft(null); }} onError={onError} /></Suspense>}
  </aside>;
}
