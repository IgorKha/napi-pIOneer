import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { api, setUnauthorizedHandler, type BenchModel, type Board } from "./api";
import { BoardPage } from "./board/BoardPage";
import { href, useRoute } from "./route";

// Monaco (~3 МБ) грузится только на странице скриптов
const ScriptsPage = lazy(() => import("./scripts/ScriptsPage").then((m) => ({ default: m.ScriptsPage })));
const ProjectsPage = lazy(() => import("./projects/ProjectsPage").then((m) => ({ default: m.ProjectsPage })));
const BlocksPage = lazy(() => import("./blocks/BlocksPage").then((m) => ({ default: m.BlocksPage })));
const HelpPage = lazy(() => import("./help/HelpPage").then((m) => ({ default: m.HelpPage })));
import { Login } from "./Login";
import { SecretsPage } from "./SecretsPage";
import { useLive, type LiveStatus } from "./useLive";

const STATUS: Record<LiveStatus, [string, string]> = {
  open: ["bg-ok", "онлайн"],
  connecting: ["bg-warn", "подключение…"],
  closed: ["bg-err", "нет связи"],
};

type AuthState = "checking" | "offline" | "in" | "out" | "no-password";

export function App() {
  const [auth, setAuth] = useState<AuthState>("checking");
  const loggedOut = useCallback(() => setAuth("out"), []);

  useEffect(() => {
    setUnauthorizedHandler(loggedOut);
    let timer: number | undefined;
    const check = () =>
      api.me().then(
        (m) => setAuth(m.authenticated ? "in" : m.password_set ? "out" : "no-password"),
        // нет связи (core перезапускается, туннель упал) — это не «вышли из системы»: ждём и повторяем
        () => { setAuth((a) => (a === "in" ? a : "offline")); timer = window.setTimeout(check, 2000); },
      );
    check();
    return () => window.clearTimeout(timer);
  }, [loggedOut]);

  if (auth === "checking") return null;
  if (auth === "offline") {
    return (
      <div className="flex h-full items-center justify-center p-4 text-muted">
        <span className="mr-2 size-2 animate-pulse rounded-full bg-warn" />
        Нет связи с платой — пробую снова…
      </div>
    );
  }
  if (auth !== "in") return <Login passwordSet={auth === "out"} onDone={() => setAuth("in")} />;
  return <Main onLogout={() => api.logout().finally(loggedOut)} onUnauthorized={loggedOut} />;
}

function Main({ onLogout, onUnauthorized }: { onLogout: () => void; onUnauthorized: () => void }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [initialBench, setInitialBench] = useState<BenchModel[] | null>(null);
  const { pins: live, bench, leds, scriptLogs, scriptStatus, status, projectLive, projectLogs, projectStatus,
          resetProjectLive } = useLive(initialBench, onUnauthorized);
  const route = useRoute();
  // плата с живыми захватами — для подсказок пинов в редакторе
  const liveBoard = useMemo(() => board && {
    ...board, pins: board.pins.map((p) => (live[p.id] ? { ...p, ...live[p.id] } : p)),
  }, [board, live]);

  useEffect(() => {
    api.board().then((b) => {
      setBoard(b);
      if (b.hal === "bench") api.bench().then((r) => setInitialBench(r.models));
    }, (e: Error) => setLoadError(e.message));
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
        {([["board", "Плата"], ["projects", "Проекты"], ["blocks", "Блоки"], ["scripts", "Скрипты"], ["secrets", "Секреты"], ["help", "Справка"]] as const).map(([page, title]) => (
          <a key={page} href={href({ page })} aria-current={route.page === page ? "page" : undefined}
             className={`rounded px-2.5 py-1 text-xs ${route.page === page ? "bg-panel-2 text-fg" : "text-muted hover:text-fg"}`}>
            {title}
          </a>
        ))}
        <div className="ml-auto flex items-center gap-3 text-xs text-muted">
          {board && (
            <span className={board.hal === "bench" ? "text-warn" : undefined}
                  title={board.hal === "bench" ? "Виртуальный стенд: железо не трогается" : undefined}>
              {board.title} · {board.hal === "bench" ? "стенд" : "железо"}
            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span className={`size-2 rounded-full ${dot}`} />
            {label}
          </span>
          <button onClick={() => confirm("Остановить все проекты и скрипты? Выходы уйдут в безопасное состояние.") &&
                                  api.panic().catch((e: Error) => showError(e.message))}
                  title="Паника: остановить всё" className="rounded border border-err/50 px-2 py-0.5 text-err hover:bg-err/15">
            Стоп всё
          </button>
          <button onClick={onLogout} className="hover:text-fg">Выйти</button>
        </div>
      </nav>

      {route.page === "projects" ? (
        liveBoard ? (
          <Suspense fallback={<div className="p-8 text-muted">Загрузка редактора…</div>}>
            <ProjectsPage board={liveBoard} selected={route.id} pinLive={live} live={projectLive} logs={projectLogs}
                          status={projectStatus} resetLive={resetProjectLive} onError={showError} />
          </Suspense>
        ) : <div className="p-8 text-muted">Загрузка…</div>
      ) : route.page === "blocks" ? (
        <Suspense fallback={<div className="p-8 text-muted">Загрузка редактора…</div>}>
          <BlocksPage board={liveBoard} selected={route.id} onError={showError} />
        </Suspense>
      ) : route.page === "secrets" ? (
        <SecretsPage onError={showError} />
      ) : route.page === "help" ? (
        <Suspense fallback={<div className="p-8 text-muted">Загрузка…</div>}>
          <HelpPage section={route.section} anchor={route.anchor} />
        </Suspense>
      ) : route.page === "scripts" ? (
        <Suspense fallback={<div className="p-8 text-muted">Загрузка редактора…</div>}>
          <ScriptsPage board={liveBoard} selected={route.id} liveLogs={scriptLogs} liveStatus={scriptStatus}
                       onError={showError} />
        </Suspense>
      ) : board ? (
        <BoardPage board={board} live={live} bench={bench} leds={leds} onError={showError} />
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
