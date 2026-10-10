import { useMemo, useState } from "react";
import type { NodeTypeSchema } from "../api";

const ORDER = ["Железо", "Логика", "Математика", "События и время", "Сеть", "Пульт", "Отладка", "Мои блоки"];
export const DND_TYPE = "application/x-pioneer-node";

/** Палитра нод: поиск, категории; перетащить на канвас или кликнуть — нода в центре вида. */
export function Palette({ types, onAdd }: { types: NodeTypeSchema[]; onAdd: (type: string) => void }) {
  const [q, setQ] = useState("");
  const groups = useMemo(() => {
    const query = q.trim().toLowerCase();
    const shown = types.filter((t) => !query || t.title.toLowerCase().includes(query) || t.doc.toLowerCase().includes(query));
    const cats = [...new Set(shown.map((t) => t.category))].sort(
      (a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99));
    return cats.map((c) => [c, shown.filter((t) => t.category === c)] as const);
  }, [types, q]);

  return (
    <aside className="flex w-48 shrink-0 flex-col border-r border-line bg-panel">
      <div className="p-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Поиск нод" aria-label="Поиск нод"
               className="w-full rounded border border-line bg-bg px-2 py-1 text-xs outline-none focus:border-accent" />
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-1.5 pb-3">
        {groups.map(([cat, items]) => (
          <div key={cat} className="mb-2">
            <div className="px-1.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted">{cat}</div>
            {items.map((t) => (
              <button key={t.type} draggable
                      onDragStart={(e) => { e.dataTransfer.setData(DND_TYPE, t.type); e.dataTransfer.effectAllowed = "copy"; }}
                      onClick={() => onAdd(t.type)}
                      title={t.doc.split("\n\n")[0]}
                      className="flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-xs text-[#d0d0d0] hover:bg-panel-2 hover:text-fg">
                <span className="size-2 shrink-0 rounded-sm" style={{ background: t.color ?? "#666" }} />
                <span className="truncate">{t.title}</span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </aside>
  );
}
