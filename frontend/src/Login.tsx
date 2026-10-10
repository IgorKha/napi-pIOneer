import { useState } from "react";
import { api, ApiError } from "./api";

export function Login({ passwordSet, onDone }: { passwordSet: boolean; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "нет связи с платой");
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-xs rounded-lg border border-line bg-panel p-6">
        <h1 className="mb-1 text-lg font-semibold tracking-tight">
          napi-p<span className="text-accent">IO</span>neer
        </h1>
        {passwordSet ? (
          <>
            <p className="mb-5 text-muted">Вход в панель управления платой</p>
            <label htmlFor="pw" className="mb-1 block text-xs text-muted">Пароль</label>
            <input id="pw" type="password" autoFocus autoComplete="current-password" value={password}
                   onChange={(e) => setPassword(e.target.value)}
                   className="mb-4 w-full rounded border border-line bg-bg px-3 py-2 outline-none focus:border-accent" />
            {error && <p role="alert" className="mb-3 text-sm text-err">{error}</p>}
            <button type="submit" disabled={busy || !password}
                    className="w-full rounded bg-accent py-2 text-white disabled:opacity-50">
              {busy ? "Проверяю…" : "Войти"}
            </button>
          </>
        ) : (
          <p className="text-muted">
            Пароль ещё не задан. Задайте его на плате:
            <code className="mt-2 block rounded bg-bg p-2 font-mono text-xs text-fg">
              python3 -m pioneer set-password
            </code>
          </p>
        )}
      </form>
    </div>
  );
}
