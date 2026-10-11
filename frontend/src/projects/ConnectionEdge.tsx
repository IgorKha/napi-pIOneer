import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from "@xyflow/react";
import { useState } from "react";
import type { insertionOptions } from "./sockets";

type Option = ReturnType<typeof insertionOptions>[number];
export type ConnectionData = {
  options: Option[];
  pin: boolean;
  remove: () => Promise<unknown>;
  insert?: (option: Option, position: [number, number]) => Promise<unknown>;
};

export function ConnectionEdge(props: EdgeProps<Edge<ConnectionData>>) {
  const [path, x, y] = getBezierPath(props);
  return <>
    <BaseEdge id={props.id} path={path} style={{ ...props.style, strokeWidth: props.selected ? 3 : props.style?.strokeWidth }} interactionWidth={20} />
    {props.selected && props.data && <EdgeLabelRenderer>
      <ConnectionActions data={props.data} x={x} y={y} />
    </EdgeLabelRenderer>}
  </>;
}

function ConnectionActions({ data, x, y }: { data: ConnectionData; x: number; y: number }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try { await action(); } finally { setBusy(false); }
  };
  const options = data.options.filter(o => `${o.schema.title} ${o.input.title} ${o.output.title}`.toLowerCase().includes(query.trim().toLowerCase()));
  return <div className="nodrag nopan absolute z-50" style={{ transform: `translate(-50%, -50%) translate(${x}px, ${y}px)`, pointerEvents: "all" }}
              onClick={e => e.stopPropagation()} onPointerDown={e => e.stopPropagation()} onKeyDown={e => { if (e.key === "Escape") setOpen(false); e.stopPropagation(); }}>
    <div className="flex w-fit gap-1 rounded border border-line bg-panel p-1 shadow-lg">
      <button aria-label="Вставить ноду в соединение" title={data.pin ? "Привязка пина: вставка ноды недоступна" : "Вставить совместимую ноду"}
              disabled={busy || data.pin} aria-expanded={open} onClick={() => setOpen(!open)}
              className="size-7 rounded text-lg hover:bg-panel-2 disabled:opacity-30">+</button>
      <button aria-label="Удалить соединение" title="Удалить соединение" disabled={busy} onClick={() => run(data.remove)}
              className="size-7 rounded hover:bg-panel-2 hover:text-err disabled:opacity-30">🗑</button>
    </div>
    {open && <div className="absolute left-1/2 top-full mt-1 w-80 -translate-x-1/2 rounded border border-line bg-panel p-2 shadow-xl" role="dialog" aria-label="Вставить совместимую ноду">
      <input autoFocus aria-label="Поиск совместимой ноды" placeholder="Поиск ноды" value={query} onChange={e => setQuery(e.target.value)}
             className="mb-2 w-full rounded border border-line bg-bg px-2 py-1 text-xs outline-none focus:border-accent" />
      <div className="max-h-60 overflow-auto">
        {options.map(o => <button key={`${o.schema.type}:${o.input.name}:${o.output.name}`} disabled={busy}
          onClick={() => run(() => data.insert!(o, [Math.round(x - 98), Math.round(y - 30)]))}
          className="block w-full rounded px-2 py-1.5 text-left text-xs hover:bg-panel-2 disabled:opacity-30">
          <span className="block">{o.schema.title}</span>
          <span className="block text-[10px] text-muted">{o.input.title} ({o.input.type}) → {o.output.title} ({o.output.type})</span>
        </button>)}
        {!options.length && <p className="p-2 text-xs text-muted">{query ? "Ничего не найдено" : "Нет нод с совместимыми входом и выходом"}</p>}
      </div>
    </div>}
  </div>;
}
