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
  overlays: string[];
  enable: string[];
  disable: string[];
  pins_to_interface: Record<string, string>;
  pins_to_gpio: string[];
  needs_reboot: boolean;
}

const API = "/pioneer/api";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
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
    const detail = typeof data.detail === "string" ? data.detail : `${res.status} ${res.statusText}`;
    if (res.status === 401 && !path.startsWith("/auth/")) onUnauthorized();
    throw new ApiError(detail, res.status);
  }
  return data as T;
}

export const api = {
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
  scriptUpdate: (id: string, patch: { name?: string; code?: string; autostart?: boolean }) =>
    call<ScriptMeta>(`/scripts/${id}`, patch, "PUT"),
  scriptDelete: (id: string) => call(`/scripts/${id}`, undefined, "DELETE"),
  scriptRun: (id: string, action: "start" | "stop" | "restart") => call<ScriptStatus>(`/scripts/${id}/${action}`, {}),
  bench: () => call<BenchInfo>("/bench"),
  benchAdd: (kind: string, config: Record<string, unknown>) =>
    call<BenchModel>("/bench/models", { kind, config }),
  benchRemove: (id: string) => call(`/bench/models/${id}`, undefined, "DELETE"),
  benchAction: (id: string, name: string, payload: Record<string, unknown> = {}) =>
    call(`/bench/models/${id}/actions/${name}`, payload),
};
