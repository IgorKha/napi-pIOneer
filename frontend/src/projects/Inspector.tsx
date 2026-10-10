import { useEffect, useState } from "react";
import type { Board, GraphNode, Issue, NodeTypeSchema, Op, ParamSchema, ProjectFull, ProjectLive } from "../api";
import { Markdown } from "../help/Markdown";
import { href } from "../route";
import { formatLive } from "./sockets";

interface Props {
  project: ProjectFull;
  node: GraphNode | null;
  schema?: NodeTypeSchema;
  board: Board;
  live?: ProjectLive;
  running: boolean;
  apply: (ops: Op[]) => void;
  onSelect: (node: string) => void;
  onMeta: (patch: { name?: string; description?: string; autostart?: boolean }) => void;
}

/** Правая панель: выбранная нода (параметры, doc, live) или проект (настройки, проблемы графа). */
export function Inspector(props: Props) {
  const { node, schema } = props;
  return (
    <aside className="w-72 shrink-0 overflow-auto border-l border-line bg-panel p-4 text-xs">
      {node && schema ? <NodePanel {...props} node={node} schema={schema} /> : <ProjectPanel {...props} />}
    </aside>
  );
}

function NodePanel({ project, node, schema, board, live, running, apply }: Props & { node: GraphNode; schema: NodeTypeSchema }) {
  const state = live?.nodes[node.id];
  return (
    <div className="flex flex-col gap-4">
      <header>
        <div className="mb-1 flex items-center gap-2">
          <span className="size-2.5 rounded-sm" style={{ background: schema.color ?? "#666" }} />
          <span className="text-muted">{schema.title}</span>
          <a href={href({ page: "help", section: "nodes", anchor: schema.id })} className="ml-auto text-accent hover:underline">справка</a>
        </div>
        <TextField label="Подпись" value={node.label ?? ""} placeholder={schema.title}
                   onCommit={(v) => apply([{ op: "rename", node: node.id, label: v }])} />
        <div className="mt-1 font-mono text-[10px] text-muted">{node.id} · {schema.type}</div>
      </header>

      {running && state?.state === "error" && (
        <div role="alert" className="rounded border border-err/50 bg-[#3a2222] px-2 py-1.5 text-[#f0b0b0]">{state.error}</div>
      )}

      {schema.id === "builtin.webhook_in" && typeof node.params.path === "string" && node.params.path && (
        <div className="rounded border border-line bg-bg px-2 py-1.5">
          <div className="text-muted">Адрес (POST, заголовок X-Pioneer-Token):</div>
          <code className="break-all font-mono text-fg">{`${location.origin}/pioneer/api/hooks/${node.params.path}`}</code>
        </div>
      )}

      {schema.id === "builtin.command" && typeof node.params.name === "string" && node.params.name && (
        <div className="rounded border border-line bg-bg px-2 py-1.5">
          <div className="text-muted">Вызов (ключ — секрет remote_api_key):</div>
          <code className="block break-all font-mono text-fg">
            {`curl -X POST -H 'X-Pioneer-Key: …' -d '{"id":"1","args":{…}}' ${location.origin}/pioneer/api/remote/cmd/${node.params.name}`}
          </code>
        </div>
      )}

      {schema.params.length > 0 && (
        <section className="flex flex-col gap-2.5">
          <h3 className="font-semibold uppercase tracking-wide text-muted">Параметры</h3>
          {schema.params.map((p) => (
            <ParamField key={p.name} p={p} value={node.params[p.name] ?? p.default} board={board} self={`project:${project.id}`}
                        onCommit={(v) => apply([{ op: "set_param", node: node.id, name: p.name, value: v }])} />
          ))}
        </section>
      )}

      {running && schema.outputs.length > 0 && (
        <section>
          <h3 className="mb-1.5 font-semibold uppercase tracking-wide text-muted">Сейчас</h3>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            {schema.outputs.map((o) => {
              const key = `${node.id}.${o.name}`;
              const v = o.type === "Event" ? live?.events[key] : live?.values[key];
              return (
                <div key={o.name} className="contents">
                  <dt className="text-muted">{o.title}</dt>
                  <dd className="font-mono">
                    {v ? (o.type === "Event" ? `×${(v as { n: number }).n}` : formatLive((v as { v: unknown }).v)) : "—"}
                    {v && "o" in v && v.o && <span className="text-muted"> · {v.o}</span>}
                  </dd>
                </div>
              );
            })}
          </dl>
        </section>
      )}

      <section className="border-t border-line pt-3">
        <Markdown source={schema.doc} />
      </section>
    </div>
  );
}

function ParamField({ p, value, board, self, onCommit }: {
  p: ParamSchema; value: unknown; board: Board; self: string; onCommit: (v: unknown) => void;
}) {
  const label = (
    <span className="mb-0.5 block text-muted" title={p.doc}>
      {p.title}{p.unit ? `, ${p.unit}` : ""}{p.required && value == null ? <span className="text-warn"> *</span> : null}
    </span>
  );
  const select = (options: [string, string][], allowEmpty: boolean) => (
    <label className="block">
      {label}
      <select value={(value as string) ?? ""} onChange={(e) => onCommit(e.target.value || null)}
              className="w-full rounded border border-line bg-bg px-1.5 py-1 outline-none focus:border-accent">
        {allowEmpty && <option value="">— не выбран —</option>}
        {options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
      </select>
      {p.doc && <span className="mt-0.5 block text-[10.5px] text-muted/80">{p.doc}</span>}
    </label>
  );

  switch (p.kind) {
    case "bool":
      return (
        <label className="flex cursor-pointer items-start gap-2" title={p.doc}>
          <input type="checkbox" checked={!!value} onChange={(e) => onCommit(e.target.checked)} className="mt-0.5 accent-accent" />
          <span>{p.title}{p.doc && <span className="block text-[10.5px] text-muted/80">{p.doc}</span>}</span>
        </label>
      );
    case "choice":
      return select((p.choices ?? []).map((c) => [c.value, c.title]), false);
    case "pin": {
      const mode = p.modes?.[0];
      const pins = board.pins.filter((x) => (mode === "adc" ? x.kind === "adc" : x.kind === "gpio"));
      return select(pins.map((x) => {
        const avail = mode && x.runtime_modes.includes(mode as never);
        // занятость другими (свой запущенный проект пин и так держит)
        const owners = x.claim ? Object.entries(x.claim.owners).filter(([o]) => o !== self).map(([, n]) => n).join(", ") : "";
        const note = !avail ? ` — ${x.function === "kernel" ? "ядро" : board.interfaces.find((i) => i.id === x.function)?.title ?? "недоступен"}`
          : owners ? ` — занят: ${owners}` : "";
        return [x.id, `${x.id} · ${x.name ?? ""}${note}`];
      }), true);
    }
    case "interface": {
      const kind = p.modes?.[0];
      const seen = new Set<string>();
      const ifaces = board.interfaces.filter((i) => i.kind === kind && !i.locked && !seen.has(i.controller) && seen.add(i.controller));
      return select(ifaces.map((i) => {
        const on = board.interfaces.find((v) => v.controller === i.controller && v.enabled);
        return [i.controller, `${i.controller} · ${on ? Object.entries(on.pins).map(([pin, r]) => `${pin} ${r}`).join(", ") : "выключен"}`];
      }), false);
    }
    case "led":
      return select(board.leds.map((l) => [l.id, l.title]), false);
    case "int":
    case "float":
      return (
        <label className="block">
          {label}
          <TextField value={String(value ?? "")} numeric onCommit={(t) => {
            const n = Number(t.replace(",", "."));
            if (Number.isFinite(n)) onCommit(p.kind === "int" ? Math.round(n) : n);
          }} />
        </label>
      );
    default:
      return (
        <label className="block">
          {label}
          <TextField value={String(value ?? "")} mono={p.kind !== "str"} onCommit={(t) => onCommit(t)} />
          {p.doc && <span className="mt-0.5 block text-[10.5px] text-muted/80">{p.doc}</span>}
        </label>
      );
  }
}

function TextField({ value, onCommit, label, placeholder, numeric, mono }: {
  value: string; onCommit: (v: string) => void; label?: string; placeholder?: string; numeric?: boolean; mono?: boolean;
}) {
  const [t, setT] = useState(value);
  useEffect(() => setT(value), [value]);
  return (
    <input value={t} placeholder={placeholder} aria-label={label} inputMode={numeric ? "decimal" : undefined}
           onChange={(e) => setT(e.target.value)}
           onBlur={() => t !== value && onCommit(t)}
           onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setT(value); }}
           className={`w-full rounded border border-line bg-bg px-1.5 py-1 outline-none focus:border-accent ${mono ? "font-mono" : ""} ${numeric ? "text-right font-mono" : ""}`} />
  );
}

function ProjectPanel({ project, onSelect, onMeta, running }: Props) {
  const v = project.validation;
  const issues: [Issue, string][] = [
    ...v.errors.map((i): [Issue, string] => [i, "text-err"]),
    ...v.incomplete.map((i): [Issue, string] => [i, "text-warn"]),
    ...v.warnings.map((i): [Issue, string] => [i, "text-muted"]),
  ];
  return (
    <div className="flex flex-col gap-4">
      <h3 className="font-semibold uppercase tracking-wide text-muted">Проект</h3>
      <label className="block">
        <span className="mb-0.5 block text-muted">Описание</span>
        <textarea defaultValue={project.description} rows={3} key={project.id}
                  onBlur={(e) => e.target.value !== project.description && onMeta({ description: e.target.value })}
                  className="w-full resize-y rounded border border-line bg-bg px-1.5 py-1 outline-none focus:border-accent" />
      </label>
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={project.autostart} onChange={(e) => onMeta({ autostart: e.target.checked })} className="accent-accent" />
        Автозапуск при старте платы
      </label>
      <section>
        <h3 className="mb-1.5 font-semibold uppercase tracking-wide text-muted">Проверка графа</h3>
        {issues.length === 0 ? (
          <p className="text-ok">Всё в порядке{running ? "" : " — можно запускать"}.</p>
        ) : (
          <ul className="space-y-1">
            {issues.map(([i, cls], k) => (
              <li key={k}>
                <button disabled={!i.node} onClick={() => i.node && onSelect(i.node)}
                        className={`text-left ${cls} ${i.node ? "hover:underline" : ""}`}>
                  {i.message}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <p className="text-[10.5px] leading-relaxed text-muted">
        Ноды — из палитры слева (перетащить или кликнуть). Провода — от выхода к входу; пин — протянуть от платы к ноде.
        Delete — удалить, ⌘Z / ⌘⇧Z — отмена и повтор.
      </p>
    </div>
  );
}
