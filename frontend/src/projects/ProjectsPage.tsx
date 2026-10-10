import { useCallback, useEffect, useState } from "react";
import { api, type Board, type NodeTypeSchema, type PinState, type ProjectLive, type ProjectLogLine, type ProjectMeta, type ProjectStatus } from "../api";
import { navigate } from "../route";
import { ProjectEditor } from "./ProjectEditor";

const DOT: Record<ProjectStatus["state"], string> = { running: "bg-ok", error: "bg-err", stopped: "bg-muted" };

interface Props {
  board: Board;
  selected?: string;
  pinLive: Record<string, PinState>;
  live: Record<string, ProjectLive>;
  logs: Record<string, ProjectLogLine[]>;
  status: Record<string, ProjectStatus>;
  resetLive: (id: string, snap: ProjectLive) => void;
  onError: (msg: string) => void;
}

let typesCache: Promise<NodeTypeSchema[]> | null = null;

/** Проекты (spec 3.2, 4): список слева, редактор графа справа. */
export function ProjectsPage({ board, selected, pinLive, live, logs, status, resetLive, onError }: Props) {
  const [list, setList] = useState<ProjectMeta[]>([]);
  const [types, setTypes] = useState<NodeTypeSchema[] | null>(null);
  const reload = useCallback(() => api.projects().then(setList, (e: Error) => onError(e.message)), [onError]);
  useEffect(() => { reload(); }, [reload]);
  useEffect(() => {
    (typesCache ??= api.nodeTypes().then((r) => r.types)).then(setTypes, (e: Error) => { typesCache = null; onError(e.message); });
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
        <ProjectEditor key={selected} id={selected} board={board} types={types} pinLive={pinLive}
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
