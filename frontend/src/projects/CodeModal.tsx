import { useState } from "react";
import type { Board } from "../api";
import { href } from "../route";
import { CodeEditor } from "../scripts/CodeEditor";

/** Код ноды «Скрипт»: Monaco в модальном окне; сохранение = операция set_param (с проверкой). */
export function CodeModal({ value, board, onClose, onSave }: {
  value: string; board: Board; onClose: () => void; onSave: (v: string) => void;
}) {
  const [code, setCode] = useState(value);
  const dirty = code !== value;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6" role="dialog" aria-label="Код скрипта"
         onKeyDown={(e) => e.key === "Escape" && !dirty && onClose()}>
      <div className="flex h-[80vh] w-full max-w-4xl flex-col rounded-lg border border-line bg-panel shadow-2xl">
        <div className="flex items-center gap-3 border-b border-line px-4 py-2 text-xs">
          <span className="font-semibold">Код скрипта</span>
          <a href={href({ page: "help", section: "blocks" })} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            как писать (как свои блоки)
          </a>
          <div className="ml-auto flex gap-1.5">
            <button onClick={() => (!dirty || confirm("Закрыть без сохранения?")) && onClose()}
                    className="rounded bg-panel-2 px-3 py-1 hover:bg-[#3a3a3a]">Отмена</button>
            <button onClick={() => onSave(code)} disabled={!dirty}
                    className="rounded bg-accent px-3 py-1 text-white disabled:opacity-40" title="⌘S">Сохранить</button>
          </div>
        </div>
        <div className="min-h-0 flex-1">
          <CodeEditor value={value} board={board} onChange={setCode} onSave={() => dirty && onSave(code)} />
        </div>
      </div>
    </div>
  );
}
