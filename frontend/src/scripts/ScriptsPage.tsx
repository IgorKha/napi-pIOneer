import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Board, type LogLine, type ScriptFull, type ScriptMeta, type ScriptStatus } from "../api";
import { errorArticle, useHelp } from "../help/useHelp";
import { href, navigate } from "../route";
import { AIPanel } from "../ai/AIPanel";
import { ai } from "../ai/api";
import { CodeWorkspace, type WorkspaceHandle } from "./CodeWorkspace";

import { loadSnapshot } from "../liveSnapshot";

const STATE: Record<ScriptStatus["state"], [string, string]> = {
  running: ["bg-ok", "работает"],
  error: ["bg-err", "ошибка"],
  exited: ["bg-muted", "завершился"],
  stopped: ["bg-muted", "остановлен"],
};

interface Props {
  socket: WebSocket | null;
  board: Board | null;
  selected?: string;
  liveLogs: Record<string, LogLine[]>;
  liveStatus: Record<string, ScriptStatus>;
  onError: (msg: string) => void;
}

export function ScriptsPage({ socket, board, selected, liveLogs, liveStatus, onError }: Props) {
  const [list, setList] = useState<ScriptMeta[]>([]);
  const [libraries, setLibraries] = useState<{id: string; name: string}[]>([]);
  const [library, setLibrary] = useState<string | null>(null);
  const [libraryName, setLibraryName] = useState("");
  const reloadLibraries = () => api.codeLibraries().then(setLibraries, (e: Error) => onError(e.message));
  useEffect(() => { reloadLibraries(); }, []);
  useEffect(() => { setLibrary(null); }, [selected]);
  const reload = useCallback(() => api.scripts().then(setList, (e: Error) => onError(e.message)), [onError]);
  useEffect(() => { reload(); }, [reload, socket]);

  // без выбранного — открыть первый
  useEffect(() => {
    if (!selected && list.length) navigate({ page: "scripts", id: list[0].id });
  }, [selected, list]);

  const create = async () => {
    try {
      const m = await api.scriptCreate("Новый скрипт");
      await reload();
      navigate({ page: "scripts", id: m.id });
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const statusOf = (m: ScriptMeta) => liveStatus[m.id] ?? m.status;

  return (
    <div className="flex min-h-0 flex-1">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-panel">
        <div className="flex items-center justify-between px-3 py-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Скрипты</h2>
          <button onClick={create} className="rounded px-2 py-0.5 text-xs text-accent hover:bg-panel-2">+ новый</button>
        </div>
        <ul className="min-h-0 flex-1 overflow-auto">
          {list.map((m) => {
            const [dot, label] = STATE[statusOf(m).state];
            return (
              <li key={m.id}>
                <a href={`#/scripts/${m.id}`}
                   className={`flex items-center gap-2 px-3 py-1.5 text-sm ${m.id === selected ? "bg-panel-2 text-fg" : "text-muted hover:text-fg"}`}>
                  <span className={`size-2 shrink-0 rounded-full ${dot}`} title={label} />
                  <span className="min-w-0 flex-1 truncate">{m.name}</span>
                  {m.autostart && <span className="text-[10px] text-muted" title="Автозапуск">авто</span>}
                </a>
              </li>
            );
          })}
          {list.length === 0 && <li className="px-3 py-2 text-xs text-muted">Пока нет скриптов</li>}
        </ul>
        <div className="border-t border-line p-3 text-xs">
          <div className="mb-2 flex justify-between text-muted"><span>Общие библиотеки</span>
            <button onClick={async () => {
              const name = libraryName.trim();
              if (!name) return;
              try { await api.codeLibraryCreate(name); await reloadLibraries(); setLibrary(name); setLibraryName(""); }
              catch (e) { onError((e as Error).message); }
            }}>+ новая</button>
          </div>
          <input aria-label="Имя новой библиотеки" placeholder="filters" value={libraryName} onChange={e => setLibraryName(e.target.value)} className="mb-2 w-full rounded border border-line bg-bg p-1" />
          {libraries.map(l => <button key={l.id} className={`block w-full rounded px-2 py-1 text-left ${library === l.id ? "bg-panel-2" : ""}`} onClick={() => setLibrary(l.id)}>{l.name}</button>)}
          {library && <button className="mt-2 text-accent" onClick={() => setLibrary(null)}>← Скрипт</button>}
        </div>
      </aside>

      {library ? <div className="min-w-0 flex-1"><CodeWorkspace key={library} kind="lib" id={library} board={board}
        onError={onError} onSaved={reloadLibraries} onDirty={() => {}} /></div> : selected ? (
        <ScriptView key={selected} id={selected} board={board} liveLog={liveLogs[selected]}
                    liveStatus={liveStatus[selected]} socket={socket} onError={onError} onListChange={reload} />
      ) : (
        <div className="flex flex-1 items-center justify-center text-muted">
          <button onClick={create} className="rounded bg-accent px-4 py-2 text-white">Создать первый скрипт</button>
        </div>
      )}
    </div>
  );
}

function ScriptView({ socket, id, board, liveLog, liveStatus, onError, onListChange }: {
  socket: WebSocket | null; id: string; board: Board | null; liveLog?: LogLine[]; liveStatus?: ScriptStatus;
  onError: (m: string) => void; onListChange: () => void;
}) {
  const [script, setScript] = useState<ScriptFull | null>(null);
  const workspace = useRef<WorkspaceHandle>(null);
  const [showAI, setShowAI] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [location, setLocation] = useState<{path: string; line: number} | null>(null);
  const [busy, setBusy] = useState(false);
  const logStart = useRef(0); // сколько строк live-лога уже было до загрузки истории

  useEffect(() => {
    return loadSnapshot(() => api.script(id), (s) => {
      setScript(s);
      logStart.current = liveLog?.length ?? 0;
    }, (e: Error) => { onError(e.message); navigate({ page: "scripts" }); });
  }, [id, socket]); // eslint-disable-line react-hooks/exhaustive-deps

  const status = liveStatus ?? script?.status;
  const running = status?.state === "running";
  // история из GET + то, что пришло по WS после загрузки
  const log = useMemo(() => [...(script?.log ?? []), ...(liveLog ?? []).slice(logStart.current)],
                      [script, liveLog]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const save = () => act(async () => {
    await workspace.current?.save();
  });
  const run = (action: "start" | "stop" | "restart") => act(async () => {
    if (dirty && action !== "stop") {
      await workspace.current?.save();
    }
    await api.scriptRun(id, action);
    onListChange();
  });
  const rename = (name: string) => act(async () => {
    if (!script || !name.trim() || name === script.name) return;
    const m = await api.scriptUpdate(id, { name });
    setScript((s) => (s ? { ...s, ...m } : s));
    onListChange();
  });
  const toggleAuto = () => act(async () => {
    if (!script) return;
    const m = await api.scriptUpdate(id, { autostart: !script.autostart });
    setScript((s) => (s ? { ...s, ...m } : s));
    onListChange();
  });
  const remove = () => {
    if (!script || !confirm(`Удалить «${script.name}»? Скрипт будет остановлен.`)) return;
    act(async () => {
      await api.scriptDelete(id);
      onListChange();
      navigate({ page: "scripts" });
    });
  };

  if (!script) return <div className="flex-1 p-6 text-muted">Загрузка…</div>;
  const [dot, label] = STATE[status!.state];

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2">
        <input defaultValue={script.name} aria-label="Название скрипта"
               onBlur={(e) => rename(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
               className="w-56 rounded border border-transparent bg-transparent px-1.5 py-1 font-semibold hover:border-line focus:border-accent focus:outline-none" />
        <span className="flex w-28 items-center gap-1.5 text-xs text-muted">
          <span className={`size-2 rounded-full ${dot}`} />{label}
        </span>
        <label className="flex items-center gap-1.5 text-xs text-muted">
          <input type="checkbox" checked={script.autostart} onChange={toggleAuto} className="accent-accent" />
          автозапуск
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted">
          <input type="checkbox" checked={script.restart_on_save} onChange={() => act(async () => {
            const m = await api.scriptUpdate(id, {restart_on_save: !script.restart_on_save});
            setScript(s => s ? {...s, ...m} : s);
          })} />перезапуск при сохранении
        </label>
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={() => setShowAI(!showAI)} aria-pressed={showAI} className={`rounded px-3 py-1 text-xs ${showAI ? "bg-accent text-white" : "bg-panel-2"}`}>AI</button>
          <button onClick={save} disabled={!dirty || busy}
                  className="rounded bg-panel-2 px-3 py-1 text-xs hover:bg-[#3a3a3a] disabled:opacity-40"
                  title="⌘S / Ctrl+S">
            {dirty ? "Сохранить •" : "Сохранено"}
          </button>
          {running ? (
            <>
              <button onClick={() => run("restart")} disabled={busy}
                      className={`rounded px-3 py-1 text-xs disabled:opacity-40 ${dirty ? "bg-accent text-white" : "bg-panel-2 hover:bg-[#3a3a3a]"}`}>
                {dirty ? "Сохранить и перезапустить" : "Перезапустить"}
              </button>
              <button onClick={() => run("stop")} disabled={busy}
                      className="rounded bg-err/80 px-3 py-1 text-xs text-white hover:bg-err disabled:opacity-40">■ Стоп</button>
            </>
          ) : (
            <button onClick={() => run("start")} disabled={busy}
                    className="rounded bg-ok/90 px-3 py-1 text-xs text-bg hover:bg-ok disabled:opacity-40">▶ Запустить</button>
          )}
          <button onClick={remove} aria-label="Удалить скрипт" title="Удалить"
                  className="px-1.5 text-muted hover:text-err">🗑</button>
        </div>
      </div>

      {status?.state === "error" && status.error && (
        <div role="alert" className="border-b border-err/40 bg-[#3a2222] px-4 py-1.5 font-mono text-xs text-[#f0b0b0]">
          {status.error}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1"><CodeWorkspace ref={workspace} kind="scripts" id={id} board={board} onError={onError}
          onDirty={setDirty} location={location} onSaved={() => { api.script(id).then(fresh => setScript(old => old ? {...fresh, log: old.log} : fresh), (e: Error) => onError(e.message)); onListChange(); }}
          onRun={async file => { await api.scriptRun(id, "stop"); await api.scriptRun(id, "start", file); onListChange(); }} /></div>
        <div className="h-full border-l border-line" style={{display: showAI ? "block" : "none"}}>
          <AIPanel id={`script:${id}`} rev={0} preview={null} onPreview={() => {}} onAccept={async () => false}
            onReference={url => { if (url.startsWith("pin:")) navigate({page: "board"}); }} onError={onError}
            beforeSend={async () => { await workspace.current?.save(); }}
            onFileSave={async (artifact, code) => {
              await workspace.current?.save();
              const result = await ai.acceptFile(`script:${id}`, artifact.id, code);
              await workspace.current?.refreshFile(result.path);
            }} />
        </div>
      </div>
      <LogPanel lines={log} onLocation={(path, line) => setLocation({path, line})} />
    </div>
  );
}

function LogPanel({ lines, onLocation }: { lines: LogLine[]; onLocation: (path: string, line: number) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true); // прилипать к низу, пока пользователь не прокрутил вверх
  const [clearedAt, setClearedAt] = useState(0);
  const shown = lines.filter((l) => l.t >= clearedAt);
  const help = useHelp();
  let anchor: string | null = null;

  useEffect(() => {
    if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [shown.length]);

  return (
    <div className="flex h-56 shrink-0 flex-col border-t border-line bg-bg">
      <div className="flex items-center gap-3 px-4 py-1 text-xs text-muted">
        <span className="font-semibold uppercase tracking-wide">Лог</span>
        <button onClick={() => setClearedAt(Date.now() / 1000)} className="ml-auto hover:text-fg">очистить</button>
      </div>
      <div ref={box} onScroll={(e) => {
             const el = e.currentTarget;
             stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 20;
           }}
           className="min-h-0 flex-1 overflow-auto px-4 pb-2 font-mono text-xs leading-relaxed" role="log" aria-live="polite">
        {shown.map((l, i) => (
          <div key={i} className={`flex gap-3 whitespace-pre-wrap ${l.stream === "err" ? "text-[#f0b0b0]" : l.stream === "sys" ? "text-muted italic" : ""}`}>
            <span className="shrink-0 text-muted/70 tabular-nums">{new Date(l.t * 1000).toLocaleTimeString("ru-RU")}</span>
            <span className="min-w-0">{(() => {
              const match = /File "([^"/][^"]*)", line (\d+)/.exec(l.text);
              return match ? <button className="text-left underline" onClick={() => onLocation(match[1], Number(match[2]))}>{l.text}</button> : l.text;
            })()}</span>
            {l.stream !== "out" && (anchor = errorArticle(help, l.text)) && (
              <a href={href({ page: "help", section: "errors", anchor })} title="Что это значит — справка"
                 className="shrink-0 rounded bg-panel-2 px-1.5 not-italic text-muted hover:text-fg">?</a>
            )}
          </div>
        ))}
        {shown.length === 0 && <span className="text-muted">Пусто. print() и log() из скрипта появятся здесь.</span>}
      </div>
    </div>
  );
}
