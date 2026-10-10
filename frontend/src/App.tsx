import { useCallback, useEffect, useState } from "react";
import { api, type Board } from "./api";
import { BoardPage } from "./board/BoardPage";
import { useLivePins, type LiveStatus } from "./useLive";

const STATUS: Record<LiveStatus, [string, string]> = {
  open: ["bg-ok", "онлайн"],
  connecting: ["bg-warn", "подключение…"],
  closed: ["bg-err", "нет связи"],
};

export function App() {
  const [board, setBoard] = useState<Board | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const { pins: live, status } = useLivePins();

  useEffect(() => {
    api.board().then(setBoard, (e: Error) => setLoadError(e.message));
  }, []);

  const showError = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 6000);
  }, []);

  const [dot, label] = STATUS[status];
  return (
    <div className="flex h-full flex-col">
      <nav className="flex h-11 shrink-0 items-center gap-5 border-b border-line bg-panel px-5">
        <span className="font-semibold tracking-tight">
          napi-p<span className="text-accent">IO</span>neer
        </span>
        <span className="rounded bg-panel-2 px-2.5 py-1 text-xs">Плата</span>
        <span className="cursor-not-allowed text-xs text-muted" title="Следующий шаг фазы 0">Скрипты</span>
        <div className="ml-auto flex items-center gap-3 text-xs text-muted">
          {board && (
            <span className={board.hal === "mock" ? "text-warn" : undefined}>
              {board.title} · HAL: {board.hal}
            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span className={`size-2 rounded-full ${dot}`} />
            {label}
          </span>
        </div>
      </nav>

      {board ? (
        <BoardPage board={board} live={live} onError={showError} />
      ) : (
        <div className="p-8 text-muted">{loadError ? `Не удалось загрузить плату: ${loadError}` : "Загрузка…"}</div>
      )}

      {toast && (
        <div role="alert"
             className="fixed right-4 bottom-4 max-w-md rounded border border-err/60 bg-[#3a2222] px-4 py-3 text-sm shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}
