import type { NodeTypeSchema, Op, GraphEdge, SocketType } from "../api";

/** Цвета сокетов в духе Blender (spec 3.3) — те же, что отдаёт /node-types. */
export const SOCKET_COLOR: Record<SocketType, string> = {
  Bool: "#c6d84a", Number: "#a1a1a1", Event: "#e5c13a", String: "#6fb6e0",
  Json: "#e07ab4", Image: "#9d7ae0", Request: "#f2f2f2", Any: "#6e6e6e",
};

const IMPLICIT: Partial<Record<SocketType, SocketType[]>> = {
  Number: ["Bool"],
  Bool: ["Number"],
  String: ["Number", "Bool"],
  Json: ["Bool", "Number", "String"],
  Any: ["Bool", "Number", "String", "Json", "Image"],
};

/** Как pioneer.engine.types.can_connect: можно ли провести провод src → dst. */
export function canConnect(src: SocketType, dst: SocketType): boolean {
  return (src === dst && src !== "Any") || (IMPLICIT[dst] ?? []).includes(src);
}

export function formatLive(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 3 : 1);
  if (typeof v === "string") return v.length > 24 ? `“${v.slice(0, 22)}…”` : `“${v}”`;
  return JSON.stringify(v).slice(0, 24);
}

export const BOARD_ID = "@board";
/** Гнездо «пин» у ноды железа: по одному на параметр-пин (у TM1637 — CLK и DIO). */
export const PIN_HANDLE = "__pin:";
export const pinHandle = (param: string) => PIN_HANDLE + param;

/** Все пары сокетов, совместимые с обоими концами разрываемого провода. */
export function insertionOptions(types: NodeTypeSchema[], source: SocketType, target: SocketType) {
  return types.flatMap(schema => schema.inputs.filter(input => canConnect(source, input.type))
    .flatMap(input => schema.outputs.filter(output => canConnect(output.type, target))
      .map(output => ({ schema, input, output }))));
}

export function insertInEdge(edge: GraphEdge, id: string, type: string, input: string, output: string, position: [number, number]): Op[] {
  return [
    { op: "disconnect", to: edge.to },
    { op: "add_node", id, type, position },
    { op: "connect", from: edge.from, to: [id, input] },
    { op: "connect", from: [id, output], to: edge.to },
  ];
}
