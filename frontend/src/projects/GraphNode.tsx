import { Handle, type NodeProps, Position, type Node } from "@xyflow/react";
import { memo, useEffect, useRef, useState } from "react";
import type { Board, GraphNode as GNode, Issue, NodeTypeSchema, ProjectLive, SocketSchema } from "../api";
import { formatLive, PIN_HANDLE, SOCKET_COLOR } from "./sockets";

export interface NodeActions {
  inline: (node: string, input: string, value: unknown) => void;
  ui: (node: string, value: unknown) => void;
  collapse: (node: string, collapsed: boolean) => void;
}

export type FlowNodeData = {
  gnode: GNode;
  schema: NodeTypeSchema | undefined;
  connected: Set<string>;
  live: ProjectLive | undefined;
  running: boolean;
  issues: Issue[];
  board: Board;
  actions: NodeActions;
};

export type FlowNode = Node<FlowNodeData, "pnode">;

const W = 196;

function SocketDot({ type, kind, id, collapsed }: { type: SocketSchema["type"]; kind: "source" | "target"; id: string; collapsed?: boolean }) {
  return (
    <Handle type={kind} position={kind === "source" ? Position.Right : Position.Left} id={id}
            className={collapsed ? "!opacity-0" : undefined}
            style={{
              width: 10, height: 10, background: SOCKET_COLOR[type], border: "1.5px solid #1d1d1d",
              borderRadius: type === "Event" ? 2 : "50%",
              transform: `translate(${kind === "source" ? "50%" : "-50%"}, -50%)${type === "Event" ? " rotate(45deg)" : ""}`,
              ...(kind === "source" ? { right: 0 } : { left: 0 }),
            }} />
  );
}

export const GraphNodeView = memo(function GraphNodeView({ data, selected }: NodeProps<FlowNode>) {
  const { gnode, schema, connected, live, running, issues, actions } = data;
  if (!schema) {
    return (
      <div className="rounded border border-err bg-[#3a2222] px-3 py-2 text-xs" style={{ width: W }}>
        Неизвестный тип <span className="font-mono">{gnode.type}</span>
      </div>
    );
  }
  const state = live?.nodes[gnode.id];
  const error = running && state?.state === "error" ? state.error : null;
  const problems = issues;
  const pinParam = schema.params.find((p) => p.kind === "pin" || p.kind === "interface" || p.kind === "led");
  const collapsed = !!gnode.collapsed;
  const border = error ? "border-err" : selected ? "border-white/80" : problems.length ? "border-warn/70" : "border-[#151515]";

  return (
    <div className={`rounded-md border bg-[#303030] text-[11.5px] shadow-lg shadow-black/40 ${border}`} style={{ width: W }}
         title={error ?? (problems.length ? problems.map((i) => i.message).join("\n") : undefined)}>
      <div className="relative flex items-center gap-1.5 rounded-t-[5px] px-2 py-1"
           style={{ background: schema.color ?? "#555", borderRadius: collapsed ? 5 : undefined }}>
        <button className="nodrag w-3 text-white/70 hover:text-white" aria-label={collapsed ? "Развернуть" : "Свернуть"}
                onClick={() => actions.collapse(gnode.id, !collapsed)}>{collapsed ? "▸" : "▾"}</button>
        <span className="min-w-0 flex-1 truncate font-medium text-white">{gnode.label || schema.title}</span>
        {running && <span className={`size-1.5 rounded-full ${error ? "bg-err" : "bg-ok"}`} />}
        {!running && problems.length > 0 && <span className="text-warn" aria-label="Есть проблемы">!</span>}
        {collapsed && (
          <>
            {pinParam?.kind === "pin" && <SocketDot type="Any" kind="target" id={PIN_HANDLE} collapsed />}
            {schema.inputs.map((s) => <SocketDot key={s.name} type={s.type} kind="target" id={s.name} collapsed />)}
            {schema.outputs.map((s) => <SocketDot key={s.name} type={s.type} kind="source" id={s.name} collapsed />)}
          </>
        )}
      </div>

      {!collapsed && (
        <div className="flex flex-col gap-0.5 py-1.5">
          {pinParam && <PinRow gnode={gnode} param={pinParam.name} kind={pinParam.kind} board={data.board} />}
          {schema.outputs.map((s) => {
            const lv = s.type === "Event" ? live?.events[`${gnode.id}.${s.name}`] : live?.values[`${gnode.id}.${s.name}`];
            return (
              <div key={s.name} className="relative flex h-[22px] items-center justify-end gap-2 px-3">
                {running && lv && (
                  <span className="truncate font-mono text-[10.5px] text-[#9fd3a8]" title={"o" in lv && lv.o ? `от ${lv.o}` : undefined}>
                    {s.type === "Event" ? `×${(lv as { n: number }).n}` : formatLive((lv as { v: unknown }).v)}
                  </span>
                )}
                <span className="text-[#d6d6d6]">{s.title}</span>
                <SocketDot type={s.type} kind="source" id={s.name} />
              </div>
            );
          })}
          <Body data={data} />
          {schema.inputs.map((s) => (
            <InputRow key={s.name} s={s} gnode={gnode} connected={connected.has(s.name)}
                      onCommit={(v) => actions.inline(gnode.id, s.name, v)} />
          ))}
          {error && <div className="mx-2 mt-1 rounded bg-err/20 px-1.5 py-1 text-[10.5px] text-[#f0b0b0]">{error}</div>}
        </div>
      )}
    </div>
  );
});

function PinRow({ gnode, param, kind, board }: { gnode: GNode; param: string; kind: string; board: Board }) {
  const v = gnode.params[param] as string | null | undefined;
  const pin = kind === "pin" && v ? board.pins.find((p) => p.id === v) : undefined;
  return (
    <div className="relative mx-2 mb-1 flex h-[22px] items-center gap-1.5 rounded bg-[#262626] px-2">
      {kind === "pin" && (
        <Handle type="target" position={Position.Left} id={PIN_HANDLE}
                style={{ width: 9, height: 9, left: -8, background: "#e6e6e6", border: "1.5px dashed #1d1d1d" }} />
      )}
      <span className="text-muted">{kind === "pin" ? "пин" : kind === "led" ? "LED" : "шина"}</span>
      {v ? (
        <span className="font-mono text-fg">{v}{pin?.name && <span className="text-muted"> · {pin.name}</span>}</span>
      ) : (
        <span className="text-warn">не выбран{kind === "pin" ? " — протяните от платы" : ""}</span>
      )}
    </div>
  );
}

function InputRow({ s, gnode, connected, onCommit }: { s: SocketSchema; gnode: GNode; connected: boolean; onCommit: (v: unknown) => void }) {
  const value = gnode.inputs[s.name] ?? s.default;
  return (
    <div className="relative flex min-h-[22px] items-center gap-2 px-3">
      <SocketDot type={s.type} kind="target" id={s.name} />
      {connected || !s.inline ? (
        <span className="text-[#d6d6d6]">{s.title}</span>
      ) : s.type === "Bool" ? (
        <label className="nodrag flex cursor-pointer items-center gap-1.5 text-[#d6d6d6]">
          <input type="checkbox" checked={!!value} onChange={(e) => onCommit(e.target.checked)} className="accent-accent" />
          {s.title}
        </label>
      ) : (
        <InlineField s={s} value={value} onCommit={onCommit} />
      )}
    </div>
  );
}

function InlineField({ s, value, onCommit }: { s: SocketSchema; value: unknown; onCommit: (v: unknown) => void }) {
  const [text, setText] = useState(String(value ?? ""));
  useEffect(() => setText(String(value ?? "")), [value]);
  const commit = () => {
    if (s.type === "Number") {
      const n = Number(text.replace(",", "."));
      if (Number.isFinite(n) && n !== value) onCommit(n);
      else setText(String(value ?? ""));
    } else if (text !== value) onCommit(text);
  };
  return (
    <label className="nodrag flex min-w-0 flex-1 items-center rounded bg-[#545454] px-1.5 py-0.5 text-[#e6e6e6] focus-within:bg-[#222]">
      <span className="shrink-0 text-[#cfcfcf]">{s.title}</span>
      <input value={text} onChange={(e) => setText(e.target.value)} onBlur={commit}
             onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setText(String(value ?? "")); }}
             inputMode={s.type === "Number" ? "decimal" : undefined}
             className="nowheel min-w-0 flex-1 bg-transparent text-right outline-none" aria-label={s.title} />
      {s.unit && <span className="ml-0.5 shrink-0 text-[10px] text-[#aaa]">{s.unit}</span>}
    </label>
  );
}

// ---------------------------------------------------------------- тело: контролы пульта и Watch
function Body({ data }: { data: FlowNodeData }) {
  const { gnode, schema, live, running, actions } = data;
  const view = live?.views[gnode.id] as Record<string, unknown> | undefined;
  const id = schema?.id;
  if (!id) return null;
  const send = (v: unknown) => actions.ui(gnode.id, v);
  const disabled = !running;
  const hint = disabled ? "Запустите проект, чтобы управлять" : undefined;

  if (id === "builtin.ui_button") {
    const pressed = !!view?.pressed;
    return (
      <div className="px-3 py-1">
        <button disabled={disabled} title={hint}
                onPointerDown={() => send({ pressed: true })} onPointerUp={() => send({ pressed: false })}
                onPointerLeave={() => pressed && send({ pressed: false })}
                className={`nodrag w-full rounded py-1 text-xs ${pressed ? "bg-accent text-white" : "bg-[#545454] text-fg hover:bg-[#606060]"} disabled:opacity-40`}>
          Нажать
        </button>
      </div>
    );
  }
  if (id === "builtin.ui_switch") {
    const on = !!(view?.value ?? gnode.params.initial);
    return (
      <div className="flex justify-center px-3 py-1">
        <button disabled={disabled} title={hint} role="switch" aria-checked={on} onClick={() => send(!on)}
                className={`nodrag relative h-5 w-9 rounded-full transition ${on ? "bg-ok" : "bg-[#545454]"} disabled:opacity-40`}>
          <span className={`absolute top-0.5 size-4 rounded-full bg-white transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
        </button>
      </div>
    );
  }
  if (id === "builtin.ui_slider") {
    const min = Number(gnode.params.min ?? 0), max = Number(gnode.params.max ?? 1), step = Number(gnode.params.step || 0.01);
    return <SliderControl value={Number(view?.value ?? gnode.params.initial ?? min)} min={min} max={max} step={step}
                          disabled={disabled} hint={hint} onChange={send} />;
  }
  if (id === "builtin.ui_number") {
    return <NumberControl value={Number(view?.value ?? gnode.params.initial ?? 0)} disabled={disabled} hint={hint} onChange={send} />;
  }
  if (id === "builtin.watch") return <WatchView view={view} running={running} />;
  return null;
}

function SliderControl({ value, min, max, step, disabled, hint, onChange }: {
  value: number; min: number; max: number; step: number; disabled: boolean; hint?: string; onChange: (v: number) => void;
}) {
  const [v, setV] = useState(value);
  const last = useRef(0);
  useEffect(() => setV(value), [value]);
  return (
    <div className="nodrag flex items-center gap-2 px-3 py-1" title={hint}>
      <input type="range" min={min} max={max} step={step} value={v} disabled={disabled}
             onChange={(e) => {
               const n = Number(e.target.value);
               setV(n);
               const now = performance.now();
               if (now - last.current > 50) { last.current = now; onChange(n); } // ≤ 20 Гц
             }}
             onPointerUp={() => onChange(v)} className="nowheel min-w-0 flex-1 accent-accent" aria-label="Значение" />
      <span className="w-10 text-right font-mono text-[10.5px]">{formatLive(v)}</span>
    </div>
  );
}

function NumberControl({ value, disabled, hint, onChange }: { value: number; disabled: boolean; hint?: string; onChange: (v: number) => void }) {
  const [t, setT] = useState(String(value));
  useEffect(() => setT(String(value)), [value]);
  return (
    <div className="px-3 py-1" title={hint}>
      <input value={t} disabled={disabled} onChange={(e) => setT(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") { const n = Number(t.replace(",", ".")); if (Number.isFinite(n)) onChange(n); } }}
             onBlur={() => { const n = Number(t.replace(",", ".")); if (Number.isFinite(n) && n !== value) onChange(n); }}
             className="nodrag w-full rounded bg-[#545454] px-1.5 py-0.5 text-right font-mono outline-none focus:bg-[#222] disabled:opacity-40"
             aria-label="Число" />
    </div>
  );
}

const HISTORY = 60;

function WatchView({ view, running }: { view: Record<string, unknown> | undefined; running: boolean }) {
  const hist = useRef<number[]>([]);
  const lastT = useRef<unknown>(null);
  const v = view?.value;
  if (view && view.t !== lastT.current) {
    lastT.current = view.t;
    const n = typeof v === "number" ? v : typeof v === "boolean" ? Number(v) : null;
    if (n !== null) hist.current = [...hist.current, n].slice(-HISTORY);
  }
  const h = hist.current;
  const lo = Math.min(...h), hi = Math.max(...h);
  const pts = h.map((y, i) => `${(i / Math.max(1, HISTORY - 1)) * 160},${28 - (hi > lo ? ((y - lo) / (hi - lo)) * 24 : 12) - 2}`).join(" ");
  return (
    <div className="mx-2 rounded bg-[#1f1f1f] px-2 py-1.5">
      <div className="truncate font-mono text-[13px] text-fg">{running && view ? formatLive(v) : "—"}</div>
      {h.length > 1 && (
        <svg viewBox="0 0 160 28" className="mt-1 h-7 w-full" preserveAspectRatio="none" aria-hidden>
          <polyline points={pts} fill="none" stroke="#6bdc7a" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
        </svg>
      )}
      {running && typeof view?.origin === "string" && <div className="truncate text-[10px] text-muted">от {view.origin}</div>}
    </div>
  );
}
