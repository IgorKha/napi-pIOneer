import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { HelpReference, NodeTypeSchema, SdkMember, SdkSymbol, SocketSchema } from "../api";
import { href, navigate } from "../route";
import { CodeBlock, Inline, Markdown, parseMarkdown } from "./Markdown";
import { slug, useHelp } from "./useHelp";

interface Props {
  section?: string;
  anchor?: string;
}

interface Entry {
  section: string;
  anchor?: string;
  title: string;
  hint: string;
  text: string;
}

/** Страница «Справка» (docs/ideas/004): руководства, справочник SDK, ноды, ошибки. */
export function HelpPage({ section = "start", anchor }: Props) {
  const ref = useHelp();
  const [query, setQuery] = useState("");
  const search = useRef<HTMLInputElement>(null);
  const main = useRef<HTMLDivElement>(null);

  // Ctrl+K / ⌘K — поиск по справке
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        search.current?.focus();
        search.current?.select();
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  useEffect(() => {
    if (!ref) return;
    const el = anchor ? document.getElementById(anchor) : null;
    if (el) el.scrollIntoView({ block: "start" });
    else main.current?.scrollTo({ top: 0 });
  }, [ref, section, anchor]);

  const entries = useMemo(() => (ref ? indexOf(ref) : []), [ref]);
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return entries
      .map((e) => ({ e, score: e.title.toLowerCase().includes(q) ? 2 : e.text.toLowerCase().includes(q) ? 1 : 0 }))
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 40)
      .map((r) => r.e);
  }, [entries, query]);

  if (!ref) return <div className="p-8 text-muted">Загрузка справки…</div>;
  const guide = ref.guides.find((g) => g.id === section);

  return (
    <div className="flex min-h-0 flex-1">
      <aside className="flex w-60 shrink-0 flex-col border-r border-line bg-panel">
        <div className="p-3">
          <input ref={search} value={query} onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={(e) => {
                   if (e.key === "Enter" && results[0]) {
                     navigate({ page: "help", section: results[0].section, anchor: results[0].anchor });
                     setQuery("");
                   }
                   if (e.key === "Escape") setQuery("");
                 }}
                 placeholder="Поиск  ⌘K" aria-label="Поиск по справке"
                 className="w-full rounded border border-line bg-bg px-2 py-1.5 text-xs outline-none focus:border-accent" />
        </div>
        <nav className="min-h-0 flex-1 overflow-auto px-2 pb-4 text-xs">
          {query ? (
            results.length ? results.map((r, i) => (
              <a key={i} href={href({ page: "help", section: r.section, anchor: r.anchor })} onClick={() => setQuery("")}
                 className="block rounded px-2 py-1.5 hover:bg-panel-2">
                <div className="text-fg">{r.title}</div>
                <div className="truncate text-muted">{r.hint}</div>
              </a>
            )) : <p className="px-2 text-muted">Ничего не найдено</p>
          ) : (
            <Toc ref_={ref} section={section} anchor={anchor} />
          )}
        </nav>
        <div className="border-t border-line px-3 py-2 text-[11px] text-muted">pioneer {ref.version} · справка из кода</div>
      </aside>

      <main ref={main} className="min-h-0 flex-1 overflow-auto px-10 py-8">
        {section === "api" ? (
          <ApiReference symbols={ref.sdk} />
        ) : section === "nodes" ? (
          <NodesReference nodes={ref.nodes} />
        ) : guide ? (
          <Markdown source={guide.markdown} />
        ) : (
          <p className="text-muted">Нет такого раздела.</p>
        )}
      </main>
    </div>
  );
}

function indexOf(ref: HelpReference): Entry[] {
  const out: Entry[] = [];
  for (const g of ref.guides) {
    for (const b of parseMarkdown(g.markdown)) {
      if (b.kind === "h" && b.level >= 2) out.push({ section: g.id, anchor: slug(b.text), title: b.text, hint: g.title, text: "" });
    }
    out.push({ section: g.id, title: g.title, hint: "раздел", text: g.markdown });
  }
  for (const s of ref.sdk) {
    out.push({ section: "api", anchor: s.name, title: s.name, hint: s.summary,
               text: [s.summary, s.text, s.hardware, ...s.members.map((m) => `${m.name} ${m.summary}`)].join(" ") });
    for (const m of s.members)
      out.push({ section: "api", anchor: `${s.name}.${m.name}`, title: `${s.name}.${m.name}`, hint: m.summary, text: m.text });
  }
  for (const n of ref.nodes)
    out.push({ section: "nodes", anchor: n.id, title: n.title, hint: `нода · ${n.category}`, text: n.doc });
  return out;
}

function Toc({ ref_, section, anchor }: { ref_: HelpReference; section: string; anchor?: string }) {
  const link = (sec: string, title: string, anc?: string, sub = false) => {
    const active = section === sec && (anc ? anchor === anc : !anchor || !sub);
    return (
      <a key={`${sec}/${anc ?? ""}`} href={href({ page: "help", section: sec, anchor: anc })}
         className={`block truncate rounded px-2 py-1 ${sub ? "pl-5" : "mt-1 font-medium"} ${active ? "bg-panel-2 text-fg" : "text-muted hover:text-fg"}`}>
        {title}
      </a>
    );
  };
  const guide = (id: string) => ref_.guides.find((g) => g.id === id);
  const categories = [...new Set(ref_.nodes.map((n) => n.category))];
  return (
    <>
      {["start", "recipes"].map((id) => guide(id) && link(id, guide(id)!.title))}
      {section === "recipes" && parseMarkdown(guide("recipes")?.markdown ?? "")
        .filter((b) => b.kind === "h" && b.level === 2)
        .map((b) => b.kind === "h" && link("recipes", b.text, slug(b.text), true))}
      {link("api", "Справочник SDK")}
      {section === "api" && ref_.sdk.map((s) => link("api", s.name, s.name, true))}
      {link("nodes", "Ноды графа")}
      {section === "nodes" && categories.map((c) => link("nodes", c, slug(c), true))}
      {["hardware", "errors"].map((id) => guide(id) && link(id, guide(id)!.title))}
      {section === "errors" && parseMarkdown(guide("errors")?.markdown ?? "")
        .filter((b) => b.kind === "h" && b.level === 2)
        .map((b) => b.kind === "h" && link("errors", b.text, slug(b.text), true))}
    </>
  );
}

// ---------------------------------------------------------------- справочник SDK
function ApiReference({ symbols }: { symbols: SdkSymbol[] }) {
  return (
    <div className="max-w-3xl">
      <h1 className="mb-1 text-xl font-semibold">Справочник SDK</h1>
      <p className="mb-6 text-muted">
        <code className="font-mono">from pioneer.sdk import …</code> — всё, что доступно скрипту. Сгенерировано из кода и описания платы.
      </p>
      {symbols.map((s) => <SymbolView key={s.name} s={s} />)}
    </div>
  );
}

function DocBody({ d, title }: { d: Omit<SdkMember, "name" | "kind" | "signature" | "params"> | SdkSymbol; title?: string }) {
  const args = Object.entries(d.args);
  return (
    <>
      <p className="my-2"><Inline text={d.summary} /></p>
      {d.text && <Markdown source={d.text} />}
      {args.length > 0 && (
        <dl className="my-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          {args.map(([name, text]) => (
            <Fragment key={name}>
              <dt className="font-mono text-[12px] text-fg">{name}</dt>
              <dd className="text-[#cfcfcf]"><Inline text={text} /></dd>
            </Fragment>
          ))}
        </dl>
      )}
      {d.returns && <p className="my-2"><span className="text-muted">Возвращает: </span><Inline text={d.returns} /></p>}
      {d.raises && <p className="my-2"><span className="text-muted">Ошибки: </span><Inline text={d.raises} /></p>}
      {d.hardware && (
        <div className="my-3 rounded border border-warn/40 bg-warn/10 px-3 py-2">
          <span className="font-medium text-warn">Железо. </span><Inline text={d.hardware} />
        </div>
      )}
      {d.example && <CodeBlock code={d.example} title={title} />}
    </>
  );
}

function SymbolView({ s }: { s: SdkSymbol }) {
  return (
    <section id={s.name} className="mb-10 scroll-mt-4 border-t border-line pt-5">
      <h2 className="flex items-baseline gap-2">
        <span className="text-base font-semibold">{s.name}</span>
        <span className="text-[11px] text-muted">{{ class: "класс", function: "функция", exception: "исключение" }[s.kind]}</span>
      </h2>
      <pre className="my-2 overflow-x-auto font-mono text-[12px] text-accent">{s.signature}</pre>
      <DocBody d={s} title={s.name} />
      {s.support && s.support.length > 0 && (
        <div className="my-3">
          <div className="mb-1.5 text-muted">На Napi-C:</div>
          <div className="flex flex-wrap gap-1.5">
            {s.support.map((p) => (
              <span key={p.id} title={[p.name, p.note].filter(Boolean).join(" · ")}
                    className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${p.available ? "bg-panel-2 text-fg" : "bg-panel text-muted line-through decoration-muted/60"}`}>
                {p.id}
              </span>
            ))}
          </div>
          {s.support.some((p) => !p.available) && (
            <p className="mt-1 text-[11px] text-muted">Зачёркнутые сейчас недоступны (наведите — почему).</p>
          )}
        </div>
      )}
      {s.members.map((m) => (
        <div key={m.name} id={`${s.name}.${m.name}`} className="mt-4 scroll-mt-4 border-l-2 border-line pl-4">
          <pre className="font-mono text-[12px] text-fg">
            .{m.signature}
            {m.kind === "property" && <span className="text-muted">{m.writable ? "  (чтение и запись)" : "  (только чтение)"}</span>}
          </pre>
          <DocBody d={m} title={`${s.name}.${m.name}`} />
        </div>
      ))}
    </section>
  );
}

// ---------------------------------------------------------------- ноды
function NodesReference({ nodes }: { nodes: NodeTypeSchema[] }) {
  const cats = [...new Set(nodes.map((n) => n.category))];
  return (
    <div className="max-w-3xl">
      <h1 className="mb-1 text-xl font-semibold">Ноды графа</h1>
      <p className="mb-6 text-muted">Встроенные ноды для проектов. Входы без провода редактируются прямо на ноде; параметры — в инспекторе.</p>
      {cats.map((c) => (
        <section key={c} id={slug(c)} className="scroll-mt-4">
          <h2 className="mt-8 mb-2 text-base font-semibold">{c}</h2>
          {nodes.filter((n) => n.category === c).map((n) => <NodeView key={n.type} n={n} />)}
        </section>
      ))}
    </div>
  );
}

const SOCKET_COLOR: Record<string, string> = {
  Bool: "#c6d84a", Number: "#a1a1a1", Event: "#e5c13a", String: "#6fb6e0", Json: "#e07ab4",
  Image: "#9d7ae0", Request: "#f2f2f2", Any: "#6e6e6e",
};

function SocketRow({ s, dir }: { s: SocketSchema; dir: "in" | "out" }) {
  return (
    <li className="flex items-baseline gap-2">
      <span className="inline-block size-2 shrink-0 translate-y-[-1px] rounded-full" style={{ background: SOCKET_COLOR[s.type] }} />
      <span className="text-fg">{s.title}</span>
      <span className="text-[11px] text-muted">{s.type}{dir === "in" && s.default !== undefined ? ` = ${String(s.default)}` : ""}{s.unit ? `, ${s.unit}` : ""}</span>
      {s.doc && <span className="text-muted">— {s.doc}</span>}
    </li>
  );
}

function NodeView({ n }: { n: NodeTypeSchema }) {
  return (
    <div id={n.id} className="mb-6 scroll-mt-4 rounded border border-line">
      <div className="flex items-baseline gap-2 rounded-t px-3 py-1.5" style={{ background: n.color ?? "#444" }}>
        <span className="font-medium text-white">{n.title}</span>
        <span className="font-mono text-[10px] text-white/60">{n.type}</span>
      </div>
      <div className="px-3 py-2">
        <Markdown source={n.doc} />
        {n.inputs.length > 0 && (
          <><div className="mt-2 text-muted">Входы</div><ul className="space-y-0.5">{n.inputs.map((s) => <SocketRow key={s.name} s={s} dir="in" />)}</ul></>
        )}
        {n.outputs.length > 0 && (
          <><div className="mt-2 text-muted">Выходы</div><ul className="space-y-0.5">{n.outputs.map((s) => <SocketRow key={s.name} s={s} dir="out" />)}</ul></>
        )}
        {n.params.length > 0 && (
          <><div className="mt-2 text-muted">Параметры</div>
            <ul className="space-y-0.5">
              {n.params.map((p) => (
                <li key={p.name}>
                  <span className="text-fg">{p.title}</span>
                  {p.default !== null && p.default !== undefined && p.default !== "" && <span className="text-[11px] text-muted"> = {String(p.default)}{p.unit ? ` ${p.unit}` : ""}</span>}
                  {p.doc && <span className="text-muted"> — {p.doc}</span>}
                </li>
              ))}
            </ul></>
        )}
        {n.breaks_cycle && <p className="mt-2 text-[11px] text-muted">Через эту ноду можно замкнуть цикл.</p>}
      </div>
    </div>
  );
}
