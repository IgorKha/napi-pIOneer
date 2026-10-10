import { useCallback, useEffect, useState } from "react";
import { api, type BlockFull, type BlockMeta, type Board } from "../api";
import { href, navigate } from "../route";
import { CodeEditor } from "../scripts/CodeEditor";

interface Props {
  board: Board | null;
  selected?: string;
  onError: (msg: string) => void;
}

/** «Мои блоки» (spec 3.6 b, 4.3): Python-ноды — код в Monaco, сохранение с проверкой схемы. */
export function BlocksPage({ board, selected, onError }: Props) {
  const [list, setList] = useState<BlockMeta[]>([]);
  const reload = useCallback(() => api.blocks().then(setList, (e: Error) => onError(e.message)), [onError]);
  useEffect(() => { reload(); }, [reload]);
  useEffect(() => {
    if (!selected && list.length) navigate({ page: "blocks", id: list[0].id });
  }, [selected, list]);

  const create = async () => {
    try {
      const b = await api.blockCreate("Мой блок");
      await reload();
      navigate({ page: "blocks", id: b.id });
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <div className="flex min-h-0 flex-1">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-panel">
        <div className="flex items-center justify-between px-3 py-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Мои блоки</h2>
          <button onClick={create} className="rounded px-2 py-0.5 text-xs text-accent hover:bg-panel-2">+ новый</button>
        </div>
        <ul className="min-h-0 flex-1 overflow-auto">
          {list.map((b) => (
            <li key={b.id}>
              <a href={href({ page: "blocks", id: b.id })}
                 className={`flex items-center gap-2 px-3 py-1.5 text-sm ${b.id === selected ? "bg-panel-2 text-fg" : "text-muted hover:text-fg"}`}>
                <span className={`size-2 shrink-0 rounded-full ${b.error ? "bg-err" : "bg-[#7a5a2e]"}`} />
                <span className="min-w-0 flex-1 truncate">{b.name}</span>
              </a>
            </li>
          ))}
          {list.length === 0 && <li className="px-3 py-2 text-xs text-muted">Пока нет блоков</li>}
        </ul>
        <p className="border-t border-line p-3 text-[11px] leading-relaxed text-muted">
          Блок — своя нода на Python. После сохранения появляется в палитре проектов (раздел из <code>category</code>).
          {" "}<a className="text-accent hover:underline" href={href({ page: "help", section: "blocks" })}>Как писать блоки</a>
        </p>
      </aside>
      {selected ? (
        <BlockView key={selected} id={selected} board={board} onError={onError} onListChange={reload} />
      ) : (
        <div className="flex flex-1 items-center justify-center text-muted">
          <button onClick={create} className="rounded bg-accent px-4 py-2 text-white">Создать первый блок</button>
        </div>
      )}
    </div>
  );
}

function BlockView({ id, board, onError, onListChange }: {
  id: string; board: Board | null; onError: (m: string) => void; onListChange: () => void;
}) {
  const [block, setBlock] = useState<BlockFull | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.block(id).then((b) => { setBlock(b); setCode(b.code); },
                       (e: Error) => { onError(e.message); navigate({ page: "blocks" }); });
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = block !== null && code !== block.code;
  const save = async (name?: string) => {
    if (!block) return;
    setBusy(true);
    try {
      const m = await api.blockSave(id, code, name);
      setBlock({ ...block, ...m, code });
      onListChange();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!block || !confirm(`Удалить блок «${block.name}»? Проекты с ним перестанут запускаться.`)) return;
    try {
      await api.blockDelete(id);
      onListChange();
      navigate({ page: "blocks" });
    } catch (e) {
      onError((e as Error).message);
    }
  };

  if (!block) return <div className="flex-1 p-6 text-muted">Загрузка…</div>;
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2">
        <input defaultValue={block.name} aria-label="Название блока"
               onBlur={(e) => e.target.value.trim() && e.target.value !== block.name && save(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
               className="w-56 rounded border border-transparent bg-transparent px-1.5 py-1 font-semibold hover:border-line focus:border-accent focus:outline-none" />
        <span className="flex flex-wrap gap-1.5 text-xs">
          {block.types.map((t) => (
            <a key={t} href={href({ page: "help", section: "nodes", anchor: t })}
               className="rounded bg-panel-2 px-1.5 py-0.5 font-mono text-muted hover:text-fg" title="Тип ноды в палитре">{t}</a>
          ))}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={() => save()} disabled={!dirty || busy} title="⌘S / Ctrl+S — сохранить и проверить"
                  className="rounded bg-accent px-3 py-1 text-xs text-white disabled:opacity-40">
            {busy ? "Проверяю…" : dirty ? "Сохранить •" : "Сохранено"}
          </button>
          <button onClick={remove} aria-label="Удалить блок" title="Удалить" className="px-1.5 text-muted hover:text-err">🗑</button>
        </div>
      </div>
      {block.error && (
        <div role="alert" className="border-b border-err/40 bg-[#3a2222] px-4 py-1.5 font-mono text-xs text-[#f0b0b0]">
          {block.error}{block.types.length > 0 && <span className="text-muted"> — в палитре остаётся прошлая версия</span>}
        </div>
      )}
      <div className="min-h-0 flex-1">
        <CodeEditor value={block.code} board={board} onChange={setCode} onSave={() => save()} />
      </div>
    </div>
  );
}
