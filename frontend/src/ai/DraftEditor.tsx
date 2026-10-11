import * as monaco from "monaco-editor/editor/editor.api.js";
import { useEffect, useRef, useState } from "react";
import "../scripts/CodeEditor"; // offline Monaco theme, workers and Python language
import { navigate } from "../route";
import type { AIArtifact } from "./api";

export function DraftEditor({ draft, onClose, onSaved, onError, onFileSave }: {
  onFileSave?: (code: string) => Promise<void>;
  draft: AIArtifact; onClose: () => void; onSaved: () => void; onError: (m: string) => void;
}) {
  const box = useRef<HTMLDivElement>(null), editor = useRef<monaco.editor.IStandaloneDiffEditor | undefined>(undefined);
  const [name, setName] = useState(draft.name ?? "Блок AI");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!box.current) return;
    const original = monaco.editor.createModel(draft.original ?? "", "python");
    const modified = monaco.editor.createModel(draft.code ?? "", "python");
    const e = monaco.editor.createDiffEditor(box.current, { theme: "pioneer-dark", automaticLayout: true, renderSideBySide: draft.kind === "file", useInlineViewWhenSpaceIsLimited: false, originalEditable: false });
    e.setModel({ original, modified }); editor.current = e;
    return () => { e.dispose(); original.dispose(); modified.dispose(); };
  }, [draft]);
  return <div role="dialog" aria-label={draft.kind === "file" ? "Изменения AI-файла" : "Черновик AI-блока"} className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
    <div className="flex h-[80vh] w-full max-w-4xl flex-col rounded-lg border border-line bg-panel">
      <div className="flex items-center gap-3 border-b border-line p-3 text-xs">
        {draft.kind === "file" ? <strong>{draft.path}</strong> : <input aria-label="Название AI-блока" value={name} onChange={(e) => setName(e.target.value)} className="rounded border border-line bg-bg px-2 py-1" />}
        <span className="text-muted">{draft.kind === "file" ? "Сохранение без запуска и перезапуска" : "Новый блок · код ещё не исполнялся"}</span>
        <button onClick={onClose} disabled={busy} className="ml-auto text-muted">Закрыть</button>
        <button disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            const code = editor.current?.getModel()?.modified.getValue() ?? draft.code ?? "";
            if (draft.kind === "file") {
              if (!onFileSave) throw new Error("Редактор кода недоступен");
              await onFileSave(code); onSaved(); return;
            }
            const check = await fetch("/pioneer/api/blocks/validate", { method: "POST", headers: { "X-Pioneer": "1", "Content-Type": "application/json" }, body: JSON.stringify({ name, code }) });
            if (!check.ok) throw new Error(`Проверка блока: HTTP ${check.status}`);
            const valid = await check.json();
            if (!valid.ok) throw new Error(valid.error ?? "Блок не прошёл проверку");
            const r = await fetch("/pioneer/api/blocks", { method: "POST", headers: { "X-Pioneer": "1", "Content-Type": "application/json" }, body: JSON.stringify({ name, code }) });
            if (!r.ok) throw new Error(`Не удалось сохранить блок: HTTP ${r.status}`);
            const block = await r.json(); window.dispatchEvent(new Event("pioneer-reference-changed")); onSaved(); navigate({ page: "blocks", id: block.id });
          } catch (e) { onError((e as Error).message); } finally { setBusy(false); }
        }} className="rounded bg-accent px-3 py-1 text-white disabled:opacity-40">{draft.kind === "file" ? "Сохранить файл" : "Проверить и сохранить"}</button>
      </div>
      {draft.kind === "file" && <div className="grid grid-cols-2 border-b border-line px-3 py-1 text-xs text-muted"><span>Исходный файл</span><span>Предложенные изменения · можно редактировать</span></div>}
      <div ref={box} className="min-h-0 flex-1" />
    </div>
  </div>;
}
