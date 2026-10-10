import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/** Секреты (spec 6): значения только записываются; в проектах — ссылки {{secret:имя}}. */
export function SecretsPage({ onError }: { onError: (msg: string) => void }) {
  const [names, setNames] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const reload = useCallback(() => api.secrets().then((r) => setNames(r.names), (e: Error) => onError(e.message)), [onError]);
  useEffect(() => { reload(); }, [reload]);

  const put = (patch: Record<string, string | null>) =>
    api.secretsUpdate(patch).then((r) => setNames(r.names), (e: Error) => onError(e.message));

  return (
    <div className="mx-auto w-full max-w-xl overflow-auto p-8 text-sm">
      <h1 className="mb-1 text-lg font-semibold">Секреты</h1>
      <p className="mb-5 text-muted">
        Токены и пароли для нод «HTTP-запрос», «Входящий webhook» и т.п. Хранятся только на плате (файл с правами 600),
        в браузер не возвращаются. В параметрах нод пишите ссылку <code className="font-mono text-fg">{"{{secret:имя}}"}</code>.
      </p>
      <form className="mb-6 flex gap-2" onSubmit={(e) => {
        e.preventDefault();
        if (!name.trim() || !value) return;
        put({ [name.trim()]: value }).then(() => { setName(""); setValue(""); });
      }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="имя (латиница, _ . -)" aria-label="Имя секрета"
               className="w-48 rounded border border-line bg-bg px-2 py-1.5 font-mono outline-none focus:border-accent" />
        <input value={value} onChange={(e) => setValue(e.target.value)} type="password" placeholder="значение" aria-label="Значение секрета"
               autoComplete="new-password"
               className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-1.5 outline-none focus:border-accent" />
        <button className="rounded bg-accent px-3 py-1.5 text-white disabled:opacity-40" disabled={!name.trim() || !value}>
          Сохранить
        </button>
      </form>
      <ul className="divide-y divide-line rounded border border-line">
        {names.map((n) => (
          <li key={n} className="flex items-center gap-3 px-3 py-2">
            <code className="font-mono">{`{{secret:${n}}}`}</code>
            <span className="text-muted">••••••</span>
            <button onClick={() => navigator.clipboard?.writeText(`{{secret:${n}}}`)} className="ml-auto text-xs text-muted hover:text-fg">
              копировать ссылку
            </button>
            <button onClick={() => confirm(`Удалить секрет «${n}»?`) && put({ [n]: null })} className="text-xs text-muted hover:text-err">
              удалить
            </button>
          </li>
        ))}
        {names.length === 0 && <li className="px-3 py-2 text-muted">Пока нет секретов</li>}
      </ul>
      <p className="mt-3 text-xs text-muted">Чтобы сменить значение — сохраните секрет с тем же именем.</p>
    </div>
  );
}
