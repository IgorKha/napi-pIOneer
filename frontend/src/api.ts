// Типы и вызовы /pioneer/api (зеркало pioneer/api/app.py).

export type RuntimeMode = "gpio_in" | "gpio_out" | "soft_pwm" | "adc";
export type ClaimMode = RuntimeMode | "i2c" | "spi" | "uart" | "led";

export interface Claim {
  mode: ClaimMode;
  owners: Record<string, string>;
}

export interface PinState {
  value: boolean | number | null;
  claim: Claim | null;
}

export interface Pin extends PinState {
  id: string;
  name: string | null;
  kind: "gpio" | "adc" | "power" | "special";
  power: "3v3" | "5v" | "gnd" | null;
  special: string | null;
  gpio: { chip: number; line: number } | null;
  adc_channel: number | null;
  default_pull: "up" | "down" | "none" | null;
  drive_ma: number | null;
  alt: string[];
  function: string | null; // "gpio" | "adc" | "kernel" | id интерфейса | null
  kernel_users: string[]; // кто в ядре держит пин (живой DT)
  runtime_modes: RuntimeMode[];
  interface_options: { id: string; role: string }[];
}

export interface Iface {
  id: string;
  controller: string; // имя для SDK без mux-варианта: uart2_m0 -> uart2
  kind: string; // uart | i2c | spi | pwm
  title: string;
  device: string | null;
  locked: boolean;
  note: string | null;
  enabled: boolean; // сейчас (живой DT на плате)
  enabled_in_uenv: boolean; // после перезагрузки
  pending: boolean;
  pins: Record<string, string>;
  conflicts: string[];
  holders: string[]; // кто вне pioneer держит порт (getty, mbusd…)
}

export interface LedState {
  on: boolean;
  trigger: string;
  claim: Claim | null;
}

export interface BoardLed extends LedState {
  id: string;
  title: string;
  color: string;
  default_trigger: string;
  triggers: string[];
}

export interface Board {
  id: string;
  title: string;
  hal: "bench" | "linux";
  headers: { id: string; rows: number; cols: number; pin1: string }[];
  adc: { vref: number; bits: number };
  overlays: string[];
  live_dt: boolean;
  interfaces: Iface[];
  leds: BoardLed[];
  pins: Pin[];
}

export interface BenchModel {
  id: string;
  kind: string;
  title: string;
  config: Record<string, unknown>;
  actions: string[];
  state: Record<string, unknown>;
}

export interface BenchKind {
  kind: string;
  title: string;
  params: Record<string, { type: string; default?: unknown; choices?: string[]; min?: number; max?: number }>;
  actions: string[];
}

export interface BenchInfo {
  enabled: boolean;
  kinds: BenchKind[];
  models: BenchModel[];
}

export interface ScriptStatus {
  state: "stopped" | "running" | "exited" | "error";
  pid: number | null;
  started: number | null;
  exit_code: number | null;
  error: string | null;
}

export interface ScriptMeta {
  id: string;
  name: string;
  autostart: boolean;
  entry: string;
  restart_on_save: boolean;
  created: number;
  updated: number;
  status: ScriptStatus;
}

export interface LogLine {
  t: number;
  stream: "out" | "err" | "sys";
  text: string;
}

export interface ScriptFull extends ScriptMeta {
  code: string;
  log: LogLine[];
}

export interface InterfacePlan {
  writable?: boolean;
  hash?: string | null;
  diff?: string;
  missing?: string[];
  claims?: Record<string, Record<string, string>>;
  overlays: string[];
  enable: string[];
  disable: string[];
  pins_to_interface: Record<string, string>;
  pins_to_gpio: string[];
  needs_reboot: boolean;
}

const API = "/pioneer/api";

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly detail?: Record<string, unknown>) {
    super(message);
  }
}

/** Вызывается при 401 — приложение показывает экран входа. */
let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(cb: () => void) {
  onUnauthorized = cb;
}

async function call<T>(path: string, body?: unknown, method?: string): Promise<T> {
  // X-Pioneer — CSRF-защита: сервер отвергает изменяющие запросы без него
  const headers: Record<string, string> = { "X-Pioneer": "1" };
  const init: RequestInit = { method: method ?? (body === undefined ? "GET" : "POST"), headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(API + path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // detail — строка или {message, errors…} (ошибки валидации графа)
    const obj = data.detail && typeof data.detail === "object" && !Array.isArray(data.detail) ? data.detail : undefined;
    const detail = typeof data.detail === "string" ? data.detail : obj?.message ?? `${res.status} ${res.statusText}`;
    if (res.status === 401 && !path.startsWith("/auth/")) onUnauthorized();
    throw new ApiError(detail, res.status, obj);
  }
  if (init.method !== "GET" && /^\/(blocks|groups|code\/blocks)(\/|$)/.test(path))
    window.dispatchEvent(new Event("pioneer-reference-changed"));
  return data as T;
}

// ---- справка (GET /help, docs/ideas/004)
export interface SdkParam {
  name: string;
  kind: string;
  type?: string;
  default?: string;
}

export interface SdkDoc {
  summary: string;
  text: string;
  args: Record<string, string>;
  returns?: string;
  raises?: string;
  example?: string;
  hardware?: string;
}

export interface SdkMember extends SdkDoc {
  name: string;
  kind: "method" | "property";
  signature: string;
  params: SdkParam[];
  snippet?: string;
  writable?: boolean;
}

export interface SdkSymbol extends SdkDoc {
  name: string;
  kind: "class" | "function" | "exception";
  signature: string;
  params: SdkParam[];
  members: SdkMember[];
  snippet: string | null;
  support?: { id: string; name: string | null; available: boolean; note: string | null }[];
}

export interface HelpReference {
  version: string;
  sdk: SdkSymbol[];
  guides: { id: string; title: string; markdown: string }[];
  errors: { pattern: string; anchor: string }[];
  nodes: NodeTypeSchema[];
}

// ---- граф (spec 3.2–3.7a)
export type SocketType = "Bool" | "Number" | "Event" | "String" | "Json" | "Image" | "Request" | "Any";

export interface SocketSchema {
  name: string;
  type: SocketType;
  title: string;
  doc: string;
  inline?: boolean;
  default?: unknown;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
}

export interface ParamSchema {
  name: string;
  kind: "bool" | "int" | "float" | "str" | "choice" | "pin" | "interface" | "led" | "cron" | "expression" | "code";
  title: string;
  doc: string;
  default: unknown;
  required: boolean;
  choices?: { value: string; title: string }[];
  modes?: string[];
  min?: number;
  max?: number;
  unit?: string;
}

export interface NodeTypeSchema {
  type: string; // builtin.and@1
  id: string;
  version: number;
  title: string;
  category: string;
  color: string | null;
  doc: string;
  breaks_cycle: boolean;
  inputs: SocketSchema[];
  outputs: SocketSchema[];
  params: ParamSchema[];
}

export interface GraphNode {
  id: string;
  type: string;
  position: [number, number];
  params: Record<string, unknown>;
  inputs: Record<string, unknown>;
  collapsed?: boolean;
  label?: string | null;
}

export interface GraphEdge {
  from: [string, string];
  to: [string, string];
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  groups: unknown[];
  view?: { board?: { position: [number, number]; collapsed: boolean } };
}

export interface Issue {
  message: string;
  node?: string;
  socket?: string;
  param?: string;
  op_index?: number;
}

export interface Validation {
  ok: boolean;
  can_start: boolean;
  errors: Issue[];
  incomplete: Issue[];
  warnings: Issue[];
}

export interface ProjectStatus {
  state: "stopped" | "running" | "error";
  error: string | null;
  started: number | null;
  rev: number | null;
  dirty: boolean;
}

export interface ProjectMeta {
  id: string;
  name: string;
  description: string;
  autostart: boolean;
  save_state: boolean;
  rev: number;
  created: number;
  updated: number;
  nodes?: number;
  status: ProjectStatus;
}

export interface ProjectLogLine {
  t: number;
  level: "info" | "error";
  node: string | null;
  text: string;
}

export interface ProjectFull extends ProjectMeta {
  graph: Graph;
  node_types: Record<string, NodeTypeSchema>; // схемы нод с сокетами из параметров (Скрипт)
  validation: Validation;
  log: ProjectLogLine[];
}

export type Op = Record<string, unknown> & { op: string };

export interface OpsResult {
  rev: number;
  inverse: Op[];
  created: string[];
  validation: Validation;
}

export interface LiveValue {
  v: unknown;
  o: string;
  t: number;
}

export interface ProjectLive {
  values: Record<string, LiveValue>; // "<node>.<socket>"
  events: Record<string, { n: number; t?: number; o?: string; p?: unknown }>;
  views: Record<string, Record<string, unknown>>;
  nodes: Record<string, { state: string; error: string | null }>;
}

export interface MqttSettings {
  enabled: boolean;
  host: string;
  port: number;
  username: string;
  password: string;
  tls: boolean;
  device_id: string;
  base: string;
}

export interface MqttState {
  settings: MqttSettings;
  status: { enabled: boolean; connected: boolean; error: string | null; broker: string; prefix: string };
}

export interface GroupFull {
  id: string;
  name: string;
  rev: number;
  type: string;
  graph: Graph;
  inputs: { name: string; title: string; type: SocketType; targets: [string, string][] }[];
  outputs: { name: string; title: string; type: SocketType; source: [string, string] }[];
  node_types: Record<string, NodeTypeSchema>;
  validation: Validation;
}

export interface BlockMeta {
  id: string;
  name: string;
  updated: number | null;
  types: string[];
  error: string | null;
}

export interface BlockFull extends BlockMeta {
  code: string;
}

export interface CodeFile { path: string; kind: "file" | "dir"; size?: number; sha256?: string }
export const codeUrl = (path: string) => `${API}/code/${path}`;
export async function codeRequest(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(codeUrl(path), { ...init, headers: { "X-Pioneer": "1", ...init.headers } });
  if (!res.ok) {
    if (res.status === 401) onUnauthorized();
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `Ошибка ${res.status}`);
  }
  if (init.method && init.method !== "GET" && path.startsWith("blocks/")) window.dispatchEvent(new Event("pioneer-reference-changed"));
  return res;
}
export const api = {
  codeLibraries: () => call<{id: string; name: string}[]>("/code/lib"),
  codeLibraryCreate: (id: string) => call<{id: string}>("/code/lib", {id, name: id}),
  codeTree: (kind: string, id: string) => call<{meta: {entry?: string}; files: CodeFile[]}>(`/code/${kind}/${id}/tree`),
  codeFile: (kind: string, id: string, path: string) => call<{text: string}>(`/code/${kind}/${id}/file?path=${encodeURIComponent(path)}`),
  codeWrite: (kind: string, id: string, path: string, text: string) => call(`/code/${kind}/${id}/file`, {path, text}, "PUT"),
  codeOp: (kind: string, id: string, op: string, path: string, target?: string) => call(`/code/${kind}/${id}/${op}`, {path, target}),
  me: () => call<{ authenticated: boolean; password_set: boolean }>("/auth/me"),
  login: (password: string) => call("/auth/login", { password }),
  logout: () => call("/auth/logout", {}),
  board: () => call<Board>("/board"),
  manual: (pin: string, mode: RuntimeMode | "release", value = false) =>
    call(`/board/pins/${pin}/manual`, { mode, value }),
  plan: (enable: string[], disable: string[]) =>
    call<InterfacePlan>("/board/interfaces/plan", { enable, disable }),
  led: (id: string, action: "on" | "off" | "trigger" | "release", trigger?: string) =>
    call<BoardLed>(`/board/leds/${id}`, { action, trigger }),
  scripts: () => call<ScriptMeta[]>("/scripts"),
  script: (id: string) => call<ScriptFull>(`/scripts/${id}`),
  scriptCreate: (name: string, code?: string) => call<ScriptMeta>("/scripts", { name, code }),
  scriptUpdate: (id: string, patch: { name?: string; code?: string; autostart?: boolean; entry?: string; restart_on_save?: boolean }) =>
    call<ScriptMeta>(`/scripts/${id}`, patch, "PUT"),
  scriptDelete: (id: string) => call(`/scripts/${id}`, undefined, "DELETE"),
  scriptRun: (id: string, action: "start" | "stop" | "restart", file?: string) => call<ScriptStatus>(`/scripts/${id}/${action}${file ? `?file=${encodeURIComponent(file)}` : ""}`, {}),
  help: () => call<HelpReference>("/help"),
  nodeTypes: () => call<{ types: NodeTypeSchema[]; socket_colors: Record<SocketType, string> }>("/node-types"),
  projects: () => call<ProjectMeta[]>("/projects"),
  project: (id: string) => call<ProjectFull>(`/projects/${id}`),
  projectCreate: (name: string, description = "", graph?: Graph) => call<ProjectMeta>("/projects", { name, description, graph }),
  projectTemplates: () => call<{templates: {id: string; name: string; description: string; graph: Graph}[]}>("/project-templates"),
  projectUpdate: (id: string, patch: { name?: string; description?: string; autostart?: boolean; save_state?: boolean }) =>
    call<ProjectMeta>(`/projects/${id}`, patch, "PUT"),
  projectDelete: (id: string) => call(`/projects/${id}`, undefined, "DELETE"),
  projectDuplicate: (id: string) => call<ProjectMeta>(`/projects/${id}/duplicate`, {}),
  projectOps: (id: string, ops: Op[], rev?: number) => call<OpsResult>(`/projects/${id}/ops`, { ops, rev }),
  projectRun: (id: string, action: "start" | "stop" | "apply") => call<ProjectStatus>(`/projects/${id}/${action}`, {}),
  projectLive: (id: string) => call<ProjectLive>(`/projects/${id}/live`),
  projectUi: (id: string, node: string, value: unknown) => call(`/projects/${id}/nodes/${node}/ui`, { value }),
  panic: () => call("/panic", {}),
  group: (gid: string) => call<GroupFull>(`/groups/${gid}`),
  groupOps: (gid: string, ops: Op[], rev?: number) => call<OpsResult>(`/groups/${gid}/ops`, { ops, rev }),
  groupRename: (gid: string, name: string) => call(`/groups/${gid}`, { name }, "PUT"),
  mqtt: () => call<MqttState>("/settings/mqtt"),
  mqttSave: (s: MqttSettings) => call<MqttState>("/settings/mqtt", s, "PUT"),
  secrets: () => call<{ names: string[] }>("/secrets"),
  secretsUpdate: (values: Record<string, string | null>) => call<{ names: string[] }>("/secrets", values, "PUT"),
  blocks: () => call<BlockMeta[]>("/blocks"),
  block: (id: string) => call<BlockFull>(`/blocks/${id}`),
  blockCreate: (name: string) => call<BlockMeta>("/blocks", { name }),
  blockSave: (id: string, code: string, name?: string) => call<BlockMeta>(`/blocks/${id}`, { code, name }, "PUT"),
  blockDelete: (id: string) => call(`/blocks/${id}`, undefined, "DELETE"),
  bench: () => call<BenchInfo>("/bench"),
  benchAdd: (kind: string, config: Record<string, unknown>) =>
    call<BenchModel>("/bench/models", { kind, config }),
  benchRemove: (id: string) => call(`/bench/models/${id}`, undefined, "DELETE"),
  benchAction: (id: string, name: string, payload: Record<string, unknown> = {}) =>
    call(`/bench/models/${id}/actions/${name}`, payload),
};
