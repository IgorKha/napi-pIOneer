import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { memo } from "react";
import type { Board, PinState } from "../api";
import { pinLook } from "../board/look";

export type BoardNodeData = {
  board: Board;
  live: Record<string, PinState>;
  used: Set<string>; // пины этого проекта
  collapsed: boolean;
  onCollapse: (c: boolean) => void;
};

export type BoardFlowNode = Node<BoardNodeData, "board">;

const ROW = 19;

/** Board-нода (spec 4.1): гребёнки в физическом расположении; с пина тянется провод к ноде железа. */
export const BoardNodeView = memo(function BoardNodeView({ data }: NodeProps<BoardFlowNode>) {
  const { board, live, used, collapsed, onCollapse } = data;
  const byId = new Map(board.pins.map((p) => [p.id, p]));
  return (
    <div className="rounded-md border border-[#151515] bg-[#262626] text-[10.5px] shadow-lg shadow-black/40">
      <div className="flex items-center gap-1.5 rounded-t-[5px] bg-[#404040] px-2 py-1">
        <button className="nodrag w-3 text-white/70 hover:text-white" onClick={() => onCollapse(!collapsed)}
                aria-label={collapsed ? "Развернуть" : "Свернуть"}>{collapsed ? "▸" : "▾"}</button>
        <span className="font-medium text-white">{board.title}</span>
        <span className="ml-auto text-[10px] text-white/50">тяните с пина на ноду</span>
      </div>
      <div className={`flex gap-5 px-3 py-2 ${collapsed ? "h-0 overflow-hidden py-0" : ""}`}>
        {board.headers.map((h) => {
          const flipped = h.pin1 === "bottom-right";
          const rows = Array.from({ length: h.rows }, (_, r) => {
            const row = flipped ? h.rows - 1 - r : r;
            // нечётные слева (pin1 top-left); у развёрнутой гребёнки — наоборот
            const a = row * 2 + 1, b = row * 2 + 2;
            return flipped ? [b, a] : [a, b];
          });
          return (
            <div key={h.id}>
              <div className="mb-1 text-center font-semibold text-fg">{h.id}</div>
              {rows.map(([l, r]) => (
                <div key={l} className="flex items-center" style={{ height: ROW }}>
                  {[l, r].map((n, side) => {
                    const pin = byId.get(`${h.id}-${n}`);
                    if (!pin) return <span key={n} className="w-[92px]" />;
                    const st = { ...pin, ...live[pin.id] };
                    const look = pinLook(board, pin, st);
                    const wireable = pin.kind === "gpio" || pin.kind === "adc";
                    const left = side === 0;
                    return (
                      <div key={n} className={`relative flex w-[92px] items-center gap-1 ${left ? "flex-row-reverse pr-1" : "pl-1"}`}
                           title={`${pin.id} · ${look.label}`}>
                        <span className={`relative size-[11px] shrink-0 rounded-full border ${used.has(pin.id) ? "ring-2 ring-accent/70" : ""}`}
                              style={{ background: look.fill, borderColor: look.stroke }}>
                          {wireable && (
                            <Handle type="source" id={`pin:${pin.id}`} position={left ? Position.Left : Position.Right}
                                    style={{ width: 11, height: 11, background: "transparent", border: "none",
                                             left: left ? -1 : undefined, right: left ? undefined : -1, top: 5 }} />
                          )}
                        </span>
                        <span className={`truncate font-mono ${look.dim ? "text-muted/70" : used.has(pin.id) ? "text-fg" : "text-[#b8b8b8]"}`}>
                          {n} {look.label.length > 9 ? look.label.slice(0, 9) + "…" : look.label}
                        </span>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
});
