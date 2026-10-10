import { useEffect, useState } from "react";
import type { BenchModel, LedState, LogLine, PinState, ProjectLive, ProjectLogLine, ProjectStatus, ScriptStatus } from "./api";

const LOG_KEEP = 1000;

export type LiveStatus = "connecting" | "open" | "closed";

/** Подписка на /pioneer/ws: состояние пинов и моделей стенда, с переподключением. */
export function useLive(initialBench: BenchModel[] | null, onUnauthorized: () => void) {
  const [pins, setPins] = useState<Record<string, PinState>>({});
  const [bench, setBench] = useState<Record<string, BenchModel>>({});
  const [leds, setLeds] = useState<Record<string, LedState>>({});
  // лог и статусы скриптов, пришедшие по WS (история до подписки — из GET /scripts/{id})
  const [scriptLogs, setScriptLogs] = useState<Record<string, LogLine[]>>({});
  const [scriptStatus, setScriptStatus] = useState<Record<string, ScriptStatus>>({});
  const [status, setStatus] = useState<LiveStatus>("connecting");
  // проекты: накопленные live-значения сокетов, лог и статусы
  const [projectLive, setProjectLive] = useState<Record<string, ProjectLive>>({});
  const [projectLogs, setProjectLogs] = useState<Record<string, ProjectLogLine[]>>({});
  const [projectStatus, setProjectStatus] = useState<Record<string, ProjectStatus>>({});
  const [socket, setSocket] = useState<WebSocket | null>(null);

  useEffect(() => {
    if (initialBench) setBench(Object.fromEntries(initialBench.map((m) => [m.id, m])));
  }, [initialBench]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      setStatus("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/pioneer/ws`);
      ws.onopen = () => { setStatus("open"); setSocket(ws); };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "pins") setPins((prev) => ({ ...prev, ...msg.pins }));
        if (msg.type === "leds") setLeds((prev) => ({ ...prev, ...msg.leds }));
        if (msg.type === "script_log")
          setScriptLogs((prev) => ({ ...prev, [msg.id]: [...(prev[msg.id] ?? []), ...msg.lines].slice(-LOG_KEEP) }));
        if (msg.type === "script_status") setScriptStatus((prev) => ({ ...prev, [msg.id]: msg.status }));
        if (msg.type === "project_live")
          setProjectLive((prev) => ({ ...prev, [msg.id]: mergeLive(prev[msg.id], msg) }));
        if (msg.type === "project_log")
          setProjectLogs((prev) => ({ ...prev, [msg.id]: [...(prev[msg.id] ?? []), ...msg.lines].slice(-LOG_KEEP) }));
        if (msg.type === "project_status") setProjectStatus((prev) => ({ ...prev, [msg.id]: msg.status }));
        if (msg.type === "bench") {
          const upd = Object.fromEntries((msg.models as BenchModel[]).map((m) => [m.id, m]));
          setBench((prev) => (msg.full ? upd : { ...prev, ...upd }));
        }
      };
      ws.onclose = (e) => {
        setStatus("closed");
        setSocket(null);
        if (e.code === 4401) return onUnauthorized(); // сессия истекла — не переподключаемся
        if (!closed) retry = window.setTimeout(connect, 1500);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [onUnauthorized]);

  const resetProjectLive = (id: string, snapshot: ProjectLive) =>
    setProjectLive((prev) => ({ ...prev, [id]: snapshot }));

  return { pins, bench, leds, scriptLogs, scriptStatus, status, projectLive, projectLogs, projectStatus,
           resetProjectLive, socket };
}

const EMPTY: ProjectLive = { values: {}, events: {}, views: {}, nodes: {} };

function mergeLive(prev: ProjectLive | undefined, upd: Partial<ProjectLive>): ProjectLive {
  const p = prev ?? EMPTY;
  return {
    values: { ...p.values, ...upd.values },
    events: { ...p.events, ...upd.events },
    views: { ...p.views, ...upd.views },
    nodes: { ...p.nodes, ...upd.nodes },
  };
}
