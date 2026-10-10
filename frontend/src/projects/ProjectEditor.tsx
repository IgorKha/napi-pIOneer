import {
  applyNodeChanges, Background, BackgroundVariant, type Connection, Controls, type Edge, type IsValidConnection,
  MiniMap, type NodeChange, ReactFlow, ReactFlowProvider, useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, ApiError, type Board, type NodeTypeSchema, type Op, type PinState, type ProjectFull, type ProjectLive,
  type ProjectLogLine, type ProjectStatus,
} from "../api";
import { errorArticle, useHelp } from "../help/useHelp";
import { href, navigate } from "../route";
import { BoardNodeView, type BoardFlowNode } from "./BoardNode";
import { type FlowNode, GraphNodeView, type NodeActions } from "./GraphNode";
import { Inspector } from "./Inspector";
import { DND_TYPE, Palette } from "./Palette";
import { BOARD_ID, canConnect, PIN_HANDLE, pinHandle, SOCKET_COLOR } from "./sockets";

const nodeTypes = { pnode: GraphNodeView, board: BoardNodeView };
type AnyNode = FlowNode | BoardFlowNode;

const STATE: Record<ProjectStatus["state"], [string, string]> = {
  running: ["bg-ok", "работает"],
  error: ["bg-err", "ошибка"],
  stopped: ["bg-muted", "остановлен"],
};

export interface EditorProps {
  id: string;
  group?: { gid: string; inst: string }; // правка содержимого группы (Tab внутрь)
  board: Board;
  types: NodeTypeSchema[];
  pinLive: Record<string, PinState>;
  live?: ProjectLive;
  liveLog?: ProjectLogLine[];
  liveStatus?: ProjectStatus;
  resetLive: (id: string, snap: ProjectLive) => void;
  onError: (msg: string) => void;
  onListChange: () => void;
}

export function ProjectEditor(props: EditorProps) {
  return (
    <ReactFlowProvider>
      <Editor {...props} />
    </ReactFlowProvider>
  );
}

/** Live проекта, увиденный изнутри экземпляра группы: ключи «inst/нода.сокет» → «нода.сокет». */
function liveInside(live: ProjectLive | undefined, inst: string): ProjectLive | undefined {
  if (!live) return live;
  const p = inst + "/";
  const strip = <T,>(rec: Record<string, T>) =>
    Object.fromEntries(Object.entries(rec).filter(([k]) => k.startsWith(p)).map(([k, v]) => [k.slice(p.length), v]));
  return { values: strip(live.values), events: strip(live.events), views: strip(live.views), nodes: strip(live.nodes) };
}

const GROUP_PREFIX = "group.";
export const groupIdOf = (type: string) => (type.startsWith(GROUP_PREFIX) ? type.slice(GROUP_PREFIX.length).split("@")[0] : null);

function Editor({ id, group, board, types, pinLive, live: projectLiveData, liveLog, liveStatus, resetLive, onError, onListChange }: EditorProps) {
  const [project, setProject] = useState<ProjectFull | null>(null);
  const [groupName, setGroupName] = useState<string | null>(null);
  const [groupTargets, setGroupTargets] = useState<[string, string][]>([]); // входы, подключённые снаружи группы
  const live = useMemo(() => (group ? liveInside(projectLiveData, group.inst) : projectLiveData), [group, projectLiveData]);
  const [nodes, setNodes] = useState<AnyNode[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const undo = useRef<Op[][]>([]);
  const redo = useRef<Op[][]>([]);
  const [, bump] = useState(0); // перерисовать кнопки undo/redo
  // лог: история из первой загрузки + то, что пришло по WS после неё (повторные load() лог не трогают)
  const [history, setHistory] = useState<{ lines: ProjectLogLine[]; liveFrom: number } | null>(null);
  const flow = useReactFlow();
  if (import.meta.env.DEV) (window as unknown as { __flow: unknown }).__flow = flow; // отладка/проверки
  const schemas = useMemo(() => new Map(types.map((t) => [t.type, t])), [types]);
  // схема конкретной ноды: у Скрипта сокеты из кода — приходят с проектом
  const schemaOf = useCallback((n: { id: string; type: string } | undefined) =>
    n ? project?.node_types?.[n.id] ?? schemas.get(n.type) : undefined, [project, schemas]);
  const status = liveStatus ?? project?.status;
  const running = status?.state === "running";

  // в режиме группы граф/ревизия/проверка — группы, статус и лог — проекта
  const load = useCallback(() => (group
    ? Promise.all([api.project(id), api.group(group.gid)]).then(([p, g]) => {
        setGroupName(g.name);
        setGroupTargets(g.inputs.flatMap((i) => i.targets));
        setProject({ ...p, graph: g.graph, rev: g.rev, node_types: g.node_types, validation: g.validation });
      })
    : api.project(id).then(setProject)
  ).catch((e: Error) => {
    onError(e.message);
    navigate({ page: "projects", id: group ? id : undefined });
  }), [id, group, onError]);

  useEffect(() => {
    undo.current = [];
    redo.current = [];
    setHistory(null);
    api.project(id).then((p) => setHistory({ lines: p.log, liveFrom: liveLog?.length ?? 0 }), () => {});
    load();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  // live-снимок при открытии запущенного проекта (дальше — дельты по WS)
  useEffect(() => {
    if (running) api.projectLive(id).then((s) => resetLive(id, s), () => {});
  }, [id, running, resetLive]);

  // ---- операции (spec 3.7a): сервер валидирует и применяет пакет атомарно
  const send = useCallback(async (ops: Op[], history: "push" | "undo" | "redo" = "push") => {
    if (!project || !ops.length) return;
    try {
      const r = group ? await api.groupOps(group.gid, ops, project.rev) : await api.projectOps(id, ops, project.rev);
      const stack = history === "undo" ? redo : undo;
      stack.current.push(r.inverse);
      if (history === "push") redo.current = [];
      bump((n) => n + 1);
      await load();
      return r;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        onError("Граф изменён в другой вкладке — загружаю актуальную версию");
        undo.current = [];
        redo.current = [];
        await load();
      } else {
        onError((e as Error).message);
        await load(); // откатить оптимистичные перемещения
      }
    }
  }, [id, group, project, load, onError]);

  const doUndo = useCallback(() => {
    const ops = undo.current.pop();
    if (ops) send(ops, "undo");
  }, [send]);
  const doRedo = useCallback(() => {
    const ops = redo.current.pop();
    if (ops) send(ops, "redo");
  }, [send]);

  const keys = useRef({ group: () => {}, tab: () => {} }); // обработчики ⌘G / Tab (заполняются ниже)
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable]")) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) doRedo(); else doUndo();
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "y") {
        e.preventDefault();
        doRedo();
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "g") {
        e.preventDefault();
        keys.current.group();
      } else if (e.key === "Tab") {
        e.preventDefault();
        keys.current.tab();
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [doUndo, doRedo]);

  const actions: NodeActions = useMemo(() => ({
    inline: (node, input, value) => send([{ op: "set_input_value", node, name: input, value }]),
    collapse: (node, collapsed) => send([{ op: "collapse", node, collapsed }]),
    ui: (node, value) => {
      api.projectUi(id, group ? `${group.inst}/${node}` : node, value).catch((e: Error) => onError(e.message));
    },
  }), [id, group, send, onError]);

  // ---- граф -> узлы/рёбра React Flow
  // параметры-пины типа ноды (провод от платы к гнезду __pin:<параметр>)
  const pinParams = useCallback((type: string) => (schemas.get(type)?.params ?? []).filter((p) => p.kind === "pin").map((p) => p.name),
                                [schemas]); // параметры-пины у динамических нод не бывают
  const paramOf = (handle: string | null | undefined) => (handle?.startsWith(PIN_HANDLE) ? handle.slice(PIN_HANDLE.length) : null);

  useEffect(() => {
    if (!project) return;
    const g = project.graph;
    const issues = [...project.validation.errors, ...project.validation.incomplete, ...project.validation.warnings];
    const used = new Set(g.nodes.flatMap((n) => pinParams(n.type).map((p) => n.params[p])).filter(Boolean) as string[]);
    const bv = g.view?.board ?? { position: [-420, 0] as [number, number], collapsed: false };
    setNodes((prev) => {
      // сохраняем объекты React Flow (measured, selected, позиция при перетаскивании): иначе каждое
      // live-обновление заставляло бы xyflow перемерять ноды
      const old = new Map(prev.map((n) => [n.id, n]));
      const keep = <T extends AnyNode>(n: T): T => {
        const o = old.get(n.id);
        return o ? ({ ...o, data: n.data, position: o.dragging ? o.position : n.position } as T) : n;
      };
      const out: AnyNode[] = [keep<BoardFlowNode>({
        id: BOARD_ID, type: "board", position: { x: bv.position[0], y: bv.position[1] }, deletable: false,
        data: { board, live: pinLive, used, collapsed: bv.collapsed,
                onCollapse: (c: boolean) => send([{ op: "collapse", node: BOARD_ID, collapsed: c }]) },
      })];
      for (const n of g.nodes) {
        out.push(keep<FlowNode>({
          id: n.id, type: "pnode", position: { x: n.position[0], y: n.position[1] },
          data: {
            gnode: n, schema: schemaOf(n), board, live, running, actions,
            connected: new Set([...g.edges.filter((e) => e.to[0] === n.id).map((e) => e.to[1]),
                                ...groupTargets.filter((t) => t[0] === n.id).map((t) => t[1])]),
            issues: issues.filter((i) => i.node === n.id),
          },
        }));
      }
      return out;
    });
  }, [project, schemaOf, board, pinLive, live, running, actions, send, pinParams, groupTargets]);

  const edges: Edge[] = useMemo(() => {
    if (!project) return [];
    const g = project.graph;
    const out: Edge[] = g.edges.map((e) => {
      const type = schemaOf(g.nodes.find((n) => n.id === e.from[0]))?.outputs.find((o) => o.name === e.from[1])?.type ?? "Any";
      return {
        id: `${e.from.join(".")}->${e.to.join(".")}`, source: e.from[0], sourceHandle: e.from[1],
        target: e.to[0], targetHandle: e.to[1],
        style: { stroke: SOCKET_COLOR[type], strokeWidth: 2, strokeDasharray: type === "Event" ? "5 4" : undefined },
      };
    });
    for (const n of g.nodes) for (const p of pinParams(n.type)) {
      const pin = n.params[p] as string | null;
      if (!pin) continue;
      const high = pinLive[pin]?.value === true;
      out.push({
        id: `pin:${n.id}:${p}`, source: BOARD_ID, sourceHandle: `pin:${pin}`, target: n.id, targetHandle: pinHandle(p),
        style: { stroke: high ? "var(--color-pin-high)" : "#8a8a8a", strokeWidth: 1.5, strokeDasharray: "2 3" },
        animated: high,
      });
    }
    return out;
  }, [project, schemaOf, pinLive, pinParams]);

  // ---- события канваса
  const onNodesChange = useCallback((changes: NodeChange<AnyNode>[]) => {
    // удаление — через onDelete (операции); остальное — локально (перемещение, выделение)
    setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type !== "remove"), ns));
    const sel = changes.find((c) => c.type === "select" && c.selected);
    if (sel && sel.type === "select") setSelected(sel.id === BOARD_ID ? null : sel.id);
    if (changes.some((c) => c.type === "select" && !c.selected) && !changes.some((c) => c.type === "select" && c.selected))
      setSelected(null);
  }, []);

  const typeOf = useCallback((nodeId: string | null, handle: string | null | undefined, dir: "out" | "in") => {
    const gn = project?.graph.nodes.find((n) => n.id === nodeId);
    const sc = schemaOf(gn);
    return (dir === "out" ? sc?.outputs : sc?.inputs)?.find((s) => s.name === handle)?.type;
  }, [project, schemaOf]);

  const isValidConnection: IsValidConnection = useCallback((c) => {
    if (c.source === BOARD_ID) {
      const gn = project?.graph.nodes.find((n) => n.id === c.target);
      return !!gn && pinParams(gn.type).includes(paramOf(c.targetHandle) ?? "");
    }
    if (paramOf(c.targetHandle) !== null || c.source === c.target) return false;
    const a = typeOf(c.source, c.sourceHandle, "out"), b = typeOf(c.target, c.targetHandle, "in");
    return !!a && !!b && canConnect(a, b);
  }, [project, typeOf, pinParams]);

  const onConnect = useCallback((c: Connection) => {
    if (c.source === BOARD_ID) {
      const p = paramOf(c.targetHandle);
      if (p) send([{ op: "set_param", node: c.target, name: p, value: c.sourceHandle!.slice(4) }]);
      return;
    }
    send([{ op: "connect", from: [c.source, c.sourceHandle], to: [c.target, c.targetHandle] }]);
  }, [send]);

  const onDelete = useCallback(({ nodes: ns, edges: es }: { nodes: AnyNode[]; edges: Edge[] }) => {
    const removed = new Set(ns.map((n) => n.id));
    const ops: Op[] = ns.filter((n) => n.id !== BOARD_ID).map((n) => ({ op: "remove_node", id: n.id }));
    for (const e of es) {
      if (removed.has(e.target)) continue;
      if (e.source === BOARD_ID) {
        const p = paramOf(e.targetHandle);
        if (p) ops.push({ op: "set_param", node: e.target, name: p, value: null });
      } else if (!removed.has(e.source)) {
        ops.push({ op: "disconnect", to: [e.target, e.targetHandle] });
      }
    }
    send(ops);
  }, [send]);

  const onNodeDragStop = useCallback((_: unknown, __: AnyNode, dragged: AnyNode[]) => {
    const ops: Op[] = dragged.map((n) => ({ op: "move", node: n.id, position: [Math.round(n.position.x), Math.round(n.position.y)] }));
    send(ops);
  }, [send]);

  const addNode = useCallback((type: string, pos?: { x: number; y: number }) => {
    const p = pos ?? flow.screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 });
    send([{ op: "add_node", type, position: [Math.round(p.x - 98), Math.round(p.y - 30)] }])
      .then((r) => r?.created[0] && setSelected(r.created[0]));
  }, [flow, send]);

  const selectNode = useCallback((nid: string) => {
    setSelected(nid);
    setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === nid })));
    const n = flow.getNode(nid);
    if (n) flow.setCenter(n.position.x + 98, n.position.y + 40, { duration: 300, zoom: flow.getZoom() });
  }, [flow]);

  // ---- запуск
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
      load();
      onListChange();
    }
  };
  const run = (action: "start" | "stop" | "apply") => act(() => api.projectRun(id, action));
  const meta = (patch: { name?: string; description?: string; autostart?: boolean }) =>
    act(() => api.projectUpdate(id, patch));
  const remove = () => {
    if (!project || !confirm(`Удалить проект «${project.name}»? Он будет остановлен.`)) return;
    act(async () => {
      await api.projectDelete(id);
      navigate({ page: "projects" });
    });
  };

  // ---- группы нод (spec 3.6 a)
  const selectedIds = nodes.filter((n) => n.selected && n.type === "pnode").map((n) => n.id);
  const selGroupNode = selectedIds.length === 1
    ? project?.graph.nodes.find((n) => n.id === selectedIds[0] && groupIdOf(n.type)) : undefined;
  const doGroup = () => {
    if (group || !selectedIds.length) return;
    const name = prompt("Название группы", "Группа");
    if (name === null) return;
    send([{ op: "group", nodes: selectedIds, name }]).then((r) => r?.created[0] && setSelected(r.created[0]));
  };
  const doUngroup = () => selGroupNode && send([{ op: "ungroup", node: selGroupNode.id }]);
  const openGroup = (nodeId?: string) => {
    const n = project?.graph.nodes.find((x) => x.id === (nodeId ?? selGroupNode?.id));
    const gid = n && groupIdOf(n.type);
    if (n && gid) navigate({ page: "projects", id, group: gid, inst: group ? `${group.inst}/${n.id}` : n.id });
  };
  const exitGroup = () => group && navigate({ page: "projects", id });
  keys.current = { group: doGroup, tab: () => (selGroupNode ? openGroup() : exitGroup()) };

  const log = useMemo(() => [...(history?.lines ?? []), ...(history ? (liveLog ?? []).slice(history.liveFrom) : [])],
                      [history, liveLog]);

  if (!project) return <div className="flex-1 p-6 text-muted">Загрузка…</div>;
  const [dot, label] = STATE[status!.state];
  const selNode = project.graph.nodes.find((n) => n.id === selected) ?? null;

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2">
        {group ? (
          <span className="flex items-center gap-1.5 text-sm">
            <button onClick={exitGroup} title="Выйти из группы (Tab)" className="text-accent hover:underline">{project.name}</button>
            <span className="text-muted">›</span>
            <input key={group.gid} defaultValue={groupName ?? ""} aria-label="Название группы"
                   onBlur={(e) => e.target.value.trim() && e.target.value !== groupName &&
                     api.groupRename(group.gid, e.target.value).then(load, (er: Error) => onError(er.message))}
                   onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                   className="w-44 rounded border border-transparent bg-transparent px-1.5 py-1 font-semibold hover:border-line focus:border-accent focus:outline-none" />
            <span className="text-[11px] text-muted" title="Изменения действуют во всех проектах с этой группой">группа · общая для проектов</span>
          </span>
        ) : (
          <input key={project.id} defaultValue={project.name} aria-label="Название проекта"
                 onBlur={(e) => e.target.value.trim() && e.target.value !== project.name && meta({ name: e.target.value })}
                 onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                 className="w-56 rounded border border-transparent bg-transparent px-1.5 py-1 font-semibold hover:border-line focus:border-accent focus:outline-none" />
        )}
        <span className="flex items-center gap-1.5 text-xs text-muted">
          <span className={`size-2 rounded-full ${dot}`} />{label}
          {running && status?.dirty && <span className="text-warn">· граф изменён</span>}
        </span>
        <div className="flex items-center gap-1 text-xs">
          <button onClick={doUndo} disabled={!undo.current.length} title="Отменить (⌘Z)"
                  className="rounded px-2 py-1 text-muted hover:bg-panel-2 hover:text-fg disabled:opacity-30">↶</button>
          <button onClick={doRedo} disabled={!redo.current.length} title="Повторить (⌘⇧Z)"
                  className="rounded px-2 py-1 text-muted hover:bg-panel-2 hover:text-fg disabled:opacity-30">↷</button>
          {!group && selectedIds.length > 0 && !selGroupNode && (
            <button onClick={doGroup} title="Сгруппировать выделенные ноды (⌘G). Выделение — Shift + рамка"
                    className="ml-2 rounded bg-panel-2 px-2 py-1 hover:bg-[#3a3a3a]">Сгруппировать ({selectedIds.length})</button>
          )}
          {selGroupNode && (
            <>
              <button onClick={() => openGroup()} title="Открыть группу (Tab)"
                      className="ml-2 rounded bg-panel-2 px-2 py-1 hover:bg-[#3a3a3a]">Открыть группу</button>
              <button onClick={doUngroup} title="Вставить содержимое группы в граф"
                      className="rounded bg-panel-2 px-2 py-1 hover:bg-[#3a3a3a]">Разгруппировать</button>
            </>
          )}
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          {running ? (
            <>
              {status?.dirty && (
                <button onClick={() => run("apply")} disabled={busy}
                        title="Перезапустить проект с текущим графом (состояние нод сбросится)"
                        className="rounded bg-accent px-3 py-1 text-xs text-white disabled:opacity-40">Применить</button>
              )}
              <button onClick={() => run("stop")} disabled={busy}
                      className="rounded bg-err/80 px-3 py-1 text-xs text-white hover:bg-err disabled:opacity-40">■ Стоп</button>
            </>
          ) : (
            <button onClick={() => run("start")} disabled={busy || !project.validation.can_start}
                    title={project.validation.can_start ? undefined : "Граф не готов — см. «Проверка графа» справа"}
                    className="rounded bg-ok/90 px-3 py-1 text-xs text-bg hover:bg-ok disabled:opacity-40">▶ Запустить</button>
          )}
          {!group && (
            <>
              <button onClick={() => act(async () => { const m = await api.projectDuplicate(id); navigate({ page: "projects", id: m.id }); })}
                      title="Дублировать" className="px-1.5 text-muted hover:text-fg">⧉</button>
              <button onClick={remove} aria-label="Удалить проект" title="Удалить" className="px-1.5 text-muted hover:text-err">🗑</button>
            </>
          )}
        </div>
      </div>

      {status?.state === "error" && status.error && (
        <div role="alert" className="border-b border-err/40 bg-[#3a2222] px-4 py-1.5 font-mono text-xs text-[#f0b0b0]">{status.error}</div>
      )}

      <div className="flex min-h-0 flex-1">
        <Palette types={types} onAdd={(t) => addNode(t)} />
        <div className="relative min-w-0 flex-1"
             onDragOver={(e) => { if (e.dataTransfer.types.includes(DND_TYPE)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } }}
             onDrop={(e) => {
               const t = e.dataTransfer.getData(DND_TYPE);
               if (!t) return;
               e.preventDefault();
               addNode(t, flow.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
             }}>
          <ReactFlow<AnyNode, Edge> nodes={nodes} edges={edges} nodeTypes={nodeTypes} colorMode="dark"
                     onNodesChange={onNodesChange} onConnect={onConnect} onDelete={onDelete}
                     onNodeDragStop={onNodeDragStop} isValidConnection={isValidConnection}
                     onNodeDoubleClick={(_, n) => n.type === "pnode" && groupIdOf((n.data as { gnode: { type: string } }).gnode.type) && openGroup(n.id)}
                     selectionKeyCode="Shift" multiSelectionKeyCode={["Meta", "Control"]}
                     onPaneClick={() => setSelected(null)}
                     deleteKeyCode={["Delete", "Backspace"]} onInit={(f) => setTimeout(() => f.fitView({ padding: 0.15, maxZoom: 1 }), 50)}
                     minZoom={0.2} maxZoom={2} proOptions={{ hideAttribution: true }}
                     defaultEdgeOptions={{ interactionWidth: 14 }}>
            <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#3a3a3a" />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable style={{ width: 150, height: 96, background: "#232323" }} nodeColor={(n) => (n.type === "board" ? "#555" : (n.data as { schema?: NodeTypeSchema }).schema?.color ?? "#666")}
                     maskColor="rgba(0,0,0,0.5)" />
          </ReactFlow>
          {project.graph.nodes.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
              <div className="rounded border border-line bg-panel/90 px-5 py-4 text-center text-xs text-muted">
                Перетащите ноду из палитры слева.<br />Пин назначается проводом от платы к ноде железа.
              </div>
            </div>
          )}
        </div>
        <Inspector project={project} node={selNode} schema={selNode ? schemaOf(selNode) : undefined} board={board}
                   live={live} running={running} apply={(ops) => send(ops)} onSelect={selectNode} onMeta={meta} />
      </div>

      <ProjectLog lines={log} onSelect={selectNode} />
    </div>
  );
}

function ProjectLog({ lines, onSelect }: { lines: ProjectLogLine[]; onSelect: (node: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [open, setOpen] = useState(true);
  const help = useHelp();
  useEffect(() => {
    if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines.length, open]);
  return (
    <div className={`flex shrink-0 flex-col border-t border-line bg-bg ${open ? "h-36" : ""}`}>
      <button onClick={() => setOpen(!open)} className="flex items-center gap-2 px-4 py-1 text-left text-xs text-muted hover:text-fg">
        <span className="font-semibold uppercase tracking-wide">Лог</span>
        <span>{open ? "▾" : "▸"}</span>
        {!open && lines.length > 0 && <span className="truncate">{lines[lines.length - 1].text}</span>}
      </button>
      {open && (
        <div ref={box} onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 20; }}
             className="min-h-0 flex-1 overflow-auto px-4 pb-2 font-mono text-xs leading-relaxed" role="log" aria-live="polite">
          {lines.map((l, i) => {
            const anchor = l.level === "error" ? errorArticle(help, l.text) : null;
            return (
              <div key={i} className={`flex gap-3 whitespace-pre-wrap ${l.level === "error" ? "text-[#f0b0b0]" : ""}`}>
                <span className="shrink-0 text-muted/70 tabular-nums">{new Date(l.t * 1000).toLocaleTimeString("ru-RU")}</span>
                {l.node && <button onClick={() => onSelect(l.node!)} className="shrink-0 text-accent hover:underline">{l.node}</button>}
                <span className="min-w-0">{l.text}</span>
                {anchor && (
                  <a href={href({ page: "help", section: "errors", anchor })} title="Что это значит — справка"
                     className="shrink-0 rounded bg-panel-2 px-1.5 text-muted hover:text-fg">?</a>
                )}
              </div>
            );
          })}
          {lines.length === 0 && <span className="text-muted">Пусто. Сюда пишут ноды «Лог» и ошибки нод.</span>}
        </div>
      )}
    </div>
  );
}
