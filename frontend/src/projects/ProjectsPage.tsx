import { useCallback, useEffect, useState } from "react";
import { api, type Board, type NodeTypeSchema, type PinState, type ProjectLive, type ProjectLogLine, type ProjectMeta, type ProjectStatus } from "../api";
import { navigate } from "../route";
import { ProjectEditor } from "./ProjectEditor";

const DOT: Record<ProjectStatus["state"], string> = { running: "bg-ok", error: "bg-err", stopped: "bg-muted" };

interface Props {
  board: Board;
  selected?: string;
  group?: { gid: string; inst: string };
  pinLive: Record<string, PinState>;
  live: Record<string, ProjectLive>;
  logs: Record<string, ProjectLogLine[]>;
  status: Record<string, ProjectStatus>;
  resetLive: (id: string, snap: ProjectLive) => void;
  onError: (msg: string) => void;
}

/** Проекты (spec 3.2, 4): список слева, редактор графа справа. */
export function ProjectsPage({ board, selected, group, pinLive, live, logs, status, resetLive, onError }: Props) {
  const [list, setList] = useState<ProjectMeta[]>([]);
  const [templates, setTemplates] = useState<Awaited<ReturnType<typeof api.projectTemplates>>["templates"]>([]);
  useEffect(() => {api.projectTemplates().then(r => setTemplates(r.templates), (e: Error) => onError(e.message));}, [onError]);
  const [types, setTypes] = useState<NodeTypeSchema[] | null>(null);
  const reload = useCallback(() => api.projects().then(setList, (e: Error) => onError(e.message)), [onError]);
  useEffect(() => { reload(); }, [reload]);
  useEffect(() => {
    // каждый раз заново: свои блоки могли измениться на странице «Блоки»
    api.nodeTypes().then((r) => setTypes(r.types), (e: Error) => onError(e.message));
  }, [onError]);

  useEffect(() => {
    if (!selected && list.length) navigate({ page: "projects", id: list[0].id });
  }, [selected, list]);

  const create = async () => {
    try {
      const m = await api.projectCreate("Новый проект");
      await reload();
      navigate({ page: "projects", id: m.id });
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <div className="flex min-h-0 flex-1">
      <aside className="flex w-52 shrink-0 flex-col border-r border-line bg-panel">
        <div className="flex items-center justify-between px-3 py-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Проекты</h2>
          <button onClick={create} className="rounded px-2 py-0.5 text-xs text-accent hover:bg-panel-2">+ новый</button>
        </div>
        <label className="mx-3 mb-2 cursor-pointer text-xs text-muted hover:text-fg">↑ Импорт ZIP с блоками
          <input type="file" accept=".zip" className="hidden" onChange={async e=>{
            const file=e.target.files?.[0];e.target.value="";if(!file)return;
            if(!confirm("Импортировать доверенный проект и Python-код? Схемы блоков проверяются с исполнением кода в отдельном worker. Проект создаётся остановленным."))return;
            try{
              if(file.size>50*1024*1024)throw new Error("Архив больше 50 МиБ");
              const r=await fetch("/pioneer/api/projects/import.zip?confirmed=true",{method:"POST",headers:{"X-Pioneer":"1"},body:file});
              const data=await r.json();if(!r.ok)throw new Error(data.detail??"Ошибка импорта");
              window.dispatchEvent(new Event("pioneer-reference-changed"));await reload();navigate({page:"projects",id:data.id});
            }catch(err){onError((err as Error).message);}
          }}/>
        </label>
        <label className="mx-3 mb-2 cursor-pointer text-xs text-muted hover:text-fg">↑ Импорт JSON
          <input type="file" accept="application/json,.json" className="hidden" onChange={async (e) => {
            const file = e.target.files?.[0]; e.target.value = "";
            if (!file) return;
            try {
              if (file.size > 1024 * 1024) throw new Error("Файл больше 1 МБ");
              const r = await fetch("/pioneer/api/projects/import", { method: "POST", headers: { "X-Pioneer": "1", "Content-Type": "application/json" }, body: await file.text() });
              const result = await r.json();
              if (!r.ok) throw new Error(typeof result.detail === "string" ? result.detail : `HTTP ${r.status}`);
              await reload(); navigate({ page: "projects", id: result.id });
            } catch (err) { onError((err as Error).message); }
          }} />
        </label>
        <label className="mx-3 mb-3 text-xs text-muted">Начать с примера
          <select aria-label="Шаблон проекта" defaultValue="" className="mt-1 w-full rounded border border-line bg-bg px-2 py-1.5" onChange={async e => {
            const t = templates.find(t => t.id === e.target.value); e.target.value = "";
            if (!t) return;
            try {const p = await api.projectCreate(t.name, t.description, t.graph); await reload(); navigate({page: "projects", id: p.id});}
            catch (err) {onError((err as Error).message);}
          }}><option value="">Выбрать…</option>{templates.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
        </label>
        <ul className="min-h-0 flex-1 overflow-auto">
          {list.map((m) => {
            const st = status[m.id] ?? m.status;
            return (
              <li key={m.id}>
                <a href={`#/projects/${m.id}`}
                   className={`flex items-center gap-2 px-3 py-1.5 text-sm ${m.id === selected ? "bg-panel-2 text-fg" : "text-muted hover:text-fg"}`}>
                  <span className={`size-2 shrink-0 rounded-full ${DOT[st.state]}`} title={st.state} />
                  <span className="min-w-0 flex-1 truncate">{m.name}</span>
                  {m.autostart && <span className="text-[10px] text-muted" title="Автозапуск">авто</span>}
                </a>
              </li>
            );
          })}
          {list.length === 0 && <li className="px-3 py-2 text-xs text-muted">Пока нет проектов</li>}
        </ul>
      </aside>

      {selected && types ? (
        <ProjectEditor key={`${selected}/${group?.inst ?? ""}`} id={selected} group={group} board={board} types={types} pinLive={pinLive}
                       live={live[selected]} liveLog={logs[selected]} liveStatus={status[selected]}
                       resetLive={resetLive} onError={onError} onListChange={reload} />
      ) : (
        <div className="flex flex-1 items-center justify-center text-muted">
          {list.length === 0 ? (
            <button onClick={create} className="rounded bg-accent px-4 py-2 text-white">Создать первый проект</button>
          ) : "Загрузка…"}
        </div>
      )}
    </div>
  );
}
