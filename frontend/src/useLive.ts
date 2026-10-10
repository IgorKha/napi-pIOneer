import { useEffect, useState } from "react";
import type { PinState } from "./api";

export type LiveStatus = "connecting" | "open" | "closed";

/** Подписка на /pioneer/ws: состояние пинов (value + claim), с переподключением. */
export function useLivePins() {
  const [pins, setPins] = useState<Record<string, PinState>>({});
  const [status, setStatus] = useState<LiveStatus>("connecting");

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      setStatus("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/pioneer/ws`);
      ws.onopen = () => setStatus("open");
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "pins") setPins((prev) => ({ ...prev, ...msg.pins }));
      };
      ws.onclose = () => {
        setStatus("closed");
        if (!closed) retry = window.setTimeout(connect, 1500);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, []);

  return { pins, status };
}
