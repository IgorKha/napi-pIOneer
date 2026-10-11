import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { api, codeRequest, codeUrl, type Board, type CodeFile } from "../api";
import { CodeEditor } from "./CodeEditor";

export interface WorkspaceHandle { save: () => Promise<void>; refreshFile: (path: string) => Promise<void> }
interface Props {
  kind: "scripts" | "blocks" | "lib"; id: string; board: Board | null;
  onError: (message: string) => void; onSaved: () => void; onDirty: (dirty: boolean) => void;
  onRun?: (file: string) => Promise<void>;
  location?: { path: string; line: number } | null;
}
interface Draft { text: string; saved: string; binary: boolean }

export const CodeWorkspace = forwardRef<WorkspaceHandle, Props>(function CodeWorkspace(
  { kind, id, board, onError, onSaved, onDirty, onRun, location }, ref,
) {
  const [definition, setDefinition] = useState<{path:string;line:number}>();
  const [files, setFiles] = useState<CodeFile[]>([]);
  const [entry, setEntry] = useState("");
  const [active, setActive] = useState("");
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [autosave, setAutosave] = useState(true);
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<{op: "file" | "mkdir" | "move" | "delete"; path: string; target: string} | null>(null);
  const current = useRef(drafts); current.current = drafts;
  const saving = useRef(false);
  const pendingSave = useRef<Promise<void>>(Promise.resolve());
  const autosaveRef = useRef(autosave); autosaveRef.current = autosave;
  const base = `${kind}/${id}`;
  const dirty = Object.values(drafts).some(d => d.text !== d.saved);
  const report = (e: unknown) => onError((e as Error).message);
  const reload = async () => {
    const t = await api.codeTree(kind, id);
    setFiles(t.files); setEntry(t.meta.entry ?? "");
    return t.meta.entry;
  };
  const open = async (path: string) => {
    if (!current.current[path]) {
      let draft: Draft;
      try {
        const f = await api.codeFile(kind, id, path);
        draft = { text: f.text, saved: f.text, binary: false };
      } catch {
        await codeRequest(`${base}/download?path=${encodeURIComponent(path)}`);
        draft = { text: "", saved: "", binary: true };
      }
      setDrafts(d => ({ ...d, [path]: draft }));
    }
    setActive(path);
  };
  useEffect(() => { reload().then(async p => { if (p) await open(p); }).catch(report); }, [base]);
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => { if (location) open(location.path).catch(report); }, [location]);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  const save = (): Promise<void> => {
    if (saving.current) return pendingSave.current;
    saving.current = true; setBusy(true);
    pendingSave.current = (async () => {
      try {
        for (const [path, d] of Object.entries(current.current)) {
          if (d.binary || d.text === d.saved) continue;
          await api.codeWrite(kind, id, path, d.text);
          setDrafts(all => ({ ...all, [path]: { ...all[path], saved: d.text } }));
        }
        await reload(); onSaved();
      } finally { saving.current = false; setBusy(false); }
    })();
    return pendingSave.current;
  };
  useEffect(() => () => {
    // Переключение проекта до debounce не должно терять правки.
    if (!autosaveRef.current) return;
    const snapshot = current.current;
    pendingSave.current.catch(() => {}).then(async () => {
      for (const [path, d] of Object.entries(snapshot)) {
        if (!d.binary && d.text !== d.saved) await api.codeWrite(kind, id, path, d.text);
      }
    }).catch(report);
  }, [base]);
  useImperativeHandle(ref, () => ({ save, refreshFile: async (path: string) => {
    const f = await api.codeFile(kind, id, path);
    setDrafts(d => ({...d, [path]: {text: f.text, saved: f.text, binary: false}}));
    setActive(path); await reload(); onSaved();
  } }));
  useEffect(() => {
    if (!autosave || !dirty || busy) return;
    const timer = setTimeout(() => save().catch(e => { setAutosave(false); report(e); }), 1200);
    return () => clearTimeout(timer);
  }, [drafts, autosave, busy]);
  const mutate = async (fn: () => Promise<unknown>, reset = false) => {
    if (saving.current) return;
    try {
      await save(); setBusy(true); await fn();
      if (reset) { setDrafts({}); setActive(""); }
      await reload(); onSaved();
    } catch (e) { report(e); }
    finally { setBusy(false); }
  };
  const create = (folder: boolean) => setAction({op: folder ? "mkdir" : "file", path: "", target: folder ? "drivers" : "drivers/helper.py"});
  const move = (path: string, target?: string) => {
    if (!target) setAction({op: "move", path, target: path});
    else if (target !== path) mutate(() => api.codeOp(kind, id, "move", path, target), true);
  };
  const applyAction = async () => {
    if (!action) return;
    const {op, path, target} = action;
    if (op === "file" && files.some(f => f.path === target)) { onError("Этот путь уже существует"); return; }
    await mutate(async () => {
      if (op === "file") { await api.codeWrite(kind, id, target, ""); await open(target); }
      else await api.codeOp(kind, id, op, op === "mkdir" ? target : path, target);
      setAction(null);
    }, op === "move" || op === "delete");
  };
  const upload = (list: FileList | null, zip = false, folder = "") => {
    if (!list?.length) return;
    mutate(async () => {
      for (const f of Array.from(list)) {
        const path = folder + (f.webkitRelativePath || f.name);
        await codeRequest(`${base}/${zip ? "import.zip" : `upload?path=${encodeURIComponent(path)}`}`, { method: "POST", body: f });
      }
    }, true);
  };
  return <div className="flex h-full min-h-0">
    <aside className="flex w-52 shrink-0 flex-col border-r border-line bg-panel text-xs">
      <div className="flex flex-wrap gap-2 border-b border-line p-2">
        {kind !== "lib" && files.some(f => f.path === "requirements.txt") && <button disabled={busy} onClick={() => {
          if (confirm("Установить колёса из requirements.txt только для этого проекта? Нужны точные версии; остановите проект перед установкой.")) mutate(async () => {
            await codeRequest(`${base}/dependencies?confirmed=true`, {method:"POST"});
          });
        }}>Установить зависимости</button>}
        <button disabled={busy} onClick={() => create(false)}>+ файл</button>
        <button disabled={busy} onClick={() => create(true)}>+ папка</button>
        <label className="cursor-pointer">Загрузить<input disabled={busy} type="file" multiple className="hidden" onChange={e => { upload(e.target.files); e.target.value = ""; }} /></label>
        <label className="cursor-pointer">Импорт ZIP<input disabled={busy} type="file" accept=".zip" className="hidden" onChange={e => { upload(e.target.files, true); e.target.value = ""; }} /></label>
        <button disabled={busy} onClick={() => mutate(async () => {
          const res = await codeRequest(`${base}/export.zip`);
          const url = URL.createObjectURL(await res.blob());
          const link = document.createElement("a"); link.href = url; link.download = `${id}.zip`; link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        })}>Экспорт ZIP</button>
      </div>
      {action && <form className="border-b border-line p-2" onSubmit={e => {e.preventDefault(); applyAction();}}>
        {action.op === "delete" ? <p className="mb-2 break-all">Удалить {action.path}?</p> : <label className="block">Путь
          <input autoFocus aria-label="Путь файла или папки" value={action.target} onChange={e => setAction({...action, target: e.target.value})}
            className="mt-1 w-full rounded border border-line bg-bg p-1" />
        </label>}
        <div className="mt-2 flex gap-3"><button disabled={busy || (!action.target && action.op !== "delete")} type="submit" className="text-accent">{action.op === "delete" ? "Удалить" : action.op === "move" ? "Переместить" : "Создать"}</button>
          <button type="button" onClick={() => setAction(null)}>Отмена</button></div>
      </form>}
      <div className="min-h-0 flex-1 overflow-auto" onDragOver={e => e.preventDefault()} onDrop={e => {
        e.preventDefault(); if (e.dataTransfer.files.length) upload(e.dataTransfer.files);
      }}>
        {files.map(f => <div key={f.path} className={`flex items-center gap-1 px-2 py-1 ${active === f.path ? "bg-panel-2" : ""}`}
          style={{ paddingLeft: 8 + (f.path.split("/").length - 1) * 12 }} draggable={!busy}
          onDragStart={e => e.dataTransfer.setData("text/pioneer-path", f.path)}
          onDragOver={e => { if (f.kind === "dir") e.preventDefault(); }}
          onDrop={e => {
            if (f.kind !== "dir") return;
            e.preventDefault(); e.stopPropagation();
            const path = e.dataTransfer.getData("text/pioneer-path");
            if (path) move(path, `${f.path}/${path.split("/").at(-1)}`);
            else upload(e.dataTransfer.files, false, f.path + "/");
          }}>
          <button className="min-w-0 flex-1 truncate text-left" title={f.path} onClick={() => f.kind === "file" && !f.oversized && open(f.path).catch(report)}>
            {f.kind === "dir" ? "▸ " : ""}{f.path.split("/").at(-1)}{f.path === entry ? " ★" : ""}{f.oversized ? " (>5 МБ, данные)" : ""}
          </button>
          <button disabled={busy} aria-label={`Переименовать ${f.path}`} onClick={() => move(f.path)}>✎</button>
          <button disabled={busy} aria-label={`Удалить ${f.path}`} onClick={() => {
            setAction({op: "delete", path: f.path, target: ""});
          }}>×</button>
        </div>)}
      </div>
      <label className="flex gap-2 border-t border-line p-2"><input type="checkbox" checked={autosave} onChange={e => setAutosave(e.target.checked)} />Автосохранение</label>
    </aside>
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex overflow-auto border-b border-line text-xs">
        {Object.entries(drafts).map(([path, d]) => <button key={path} onClick={() => setActive(path)} className={`shrink-0 px-3 py-2 ${active === path ? "bg-panel-2" : "text-muted"}`}>
          {path}{d.text !== d.saved ? " •" : ""}
        </button>)}
      </div>
      {active && <div className="flex gap-3 border-b border-line px-3 py-1 text-xs text-muted">
        <a download={active.split("/").at(-1)} href={codeUrl(`${base}/download?path=${encodeURIComponent(active)}`)}>Скачать</a>
        {kind === "scripts" && active.endsWith(".py") && <>
          <button disabled={busy} onClick={() => mutate(() => api.scriptUpdate(id, { entry: active }))}>{active === entry ? "Точка входа" : "Сделать точкой входа"}</button>
          <button disabled={busy} onClick={() => mutate(async () => onRun?.(active))}>▶ Этот файл</button>
        </>}
        {busy && <span>Сохраняю…</span>}
      </div>}
      <div className="min-h-0 flex-1">
        {Object.entries(drafts).map(([path, d]) => <div key={path} className="h-full" style={{ display: path === active ? "block" : "none" }}>
          {d.binary ? <div className="p-4 text-xs text-muted">
            {/\.(png|jpe?g|gif|webp)$/i.test(path) ? <img className="max-h-96 max-w-full" alt={path} src={codeUrl(`${base}/download?path=${encodeURIComponent(path)}`)} /> : "Бинарный файл — доступен для скачивания"}
          </div> : <CodeEditor analysis={{kind,id,path}} onDefinition={loc=>{setDefinition(loc);open(loc.path).catch(report);}} value={d.text} board={board} language={path.endsWith(".py") ? "python" : "plaintext"}
            line={definition?.path === path ? definition.line : location?.path === path ? location.line : undefined}
            onChange={text => setDrafts(all => ({ ...all, [path]: { ...all[path], text } }))} onSave={() => save().catch(report)} />}
        </div>)}
      </div>
    </div>
  </div>;
});
