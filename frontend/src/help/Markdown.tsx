import { Fragment, type ReactNode, useState } from "react";
import { api } from "../api";
import { navigate } from "../route";
import { slug } from "./useHelp";

/** Мини-markdown для docs/sdk: заголовки, абзацы, списки, код, `inline`, **жирный**, ссылки.
 * Без зависимостей — справка должна работать офлайн и весить немного. */
import { guideLink, parseMarkdown } from "./content";
export { parseMarkdown } from "./content";

export function Inline({ text, onReference }: { text: string; onReference?: (url: string) => void }) {
  const parts: ReactNode[] = [];
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    const section = location.hash.startsWith("#/help/") ? location.hash.split("/")[2] : undefined;
    const url = m[4] ? guideLink(m[4], section) : "";
    if (m.index > last) parts.push(text.slice(last, m.index));
    if (m[1] !== undefined) parts.push(<code key={m.index} className="rounded bg-panel-2 px-1 font-mono text-[12px]">{m[1]}</code>);
    else if (m[2] !== undefined) parts.push(<strong key={m.index} className="text-fg">{m[2]}</strong>);
    else if (/^(node|pin):/.test(m[4]) && onReference)
      parts.push(<button key={m.index} onClick={() => onReference(url)} className="text-accent hover:underline">{m[3]}</button>);
    else if (/^(https?:\/\/|#|\/|\.\/|\.\.\/)/.test(url) && !url.startsWith("//"))
      parts.push(<a key={m.index} href={url} rel="noreferrer" className="text-accent hover:underline">{m[3]}</a>);
    else parts.push(m[3]);
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts.map((p, i) => <Fragment key={i}>{p}</Fragment>)}</>;
}

export function CodeBlock({ code, lang = "python", title }: { code: string; lang?: string; title?: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const runnable = lang === "python" && /from pioneer\.sdk import/.test(code);
  const open = () => {
    setBusy(true); setError("");
    api.scriptCreate(title ? `Пример: ${title}` : "Пример из справки", code + "\n")
      .then((s) => navigate({ page: "scripts", id: s.id }))
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };
  return (
    <div className="group relative my-3 rounded border border-line bg-[#191919]">
      {error && <p role="alert" className="px-3 pt-2 text-err">{error}</p>}
      <pre className="overflow-x-auto p-3 font-mono text-[12px] leading-relaxed">{code}</pre>
      <div className="absolute top-1.5 right-1.5 flex gap-1 opacity-0 transition group-hover:opacity-100">
        <button className="rounded bg-panel-2 px-2 py-0.5 text-[11px] text-muted hover:text-fg"
                onClick={() => navigator.clipboard?.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); }).catch((e: Error) => setError(e.message))}>
          {copied ? "скопировано" : "копировать"}
        </button>
        {runnable && (
          <button disabled={busy} onClick={open}
                  className="rounded bg-accent/80 px-2 py-0.5 text-[11px] text-white hover:bg-accent disabled:opacity-50"
                  title="Создать скрипт с этим кодом и открыть в редакторе">
            Открыть в редакторе
          </button>
        )}
      </div>
    </div>
  );
}

export function Markdown({ source, onReference }: { source: string; onReference?: (url: string) => void }) {
  const blocks = parseMarkdown(source);
  let heading = "";
  return (
    <div className="max-w-3xl leading-relaxed text-[#cfcfcf]">
      {blocks.map((b, i) => {
        switch (b.kind) {
          case "h": {
            heading = b.text;
            const cls = ["", "mb-4 text-xl font-semibold text-fg", "mt-8 mb-2 text-base font-semibold text-fg",
                         "mt-5 mb-1.5 font-semibold text-fg", "mt-4 font-semibold"][b.level];
            const Tag = (["h1", "h1", "h2", "h3", "h4"] as const)[b.level];
            return <Tag key={i} id={slug(b.text)} className={`scroll-mt-4 ${cls}`}><Inline text={b.text} onReference={onReference} /></Tag>;
          }
          case "quote":
            return <blockquote key={i} className="my-3 border-l-2 border-accent bg-panel px-4 py-2 text-muted"><Inline text={b.text} onReference={onReference} /></blockquote>;
          case "table":
            return <div key={i} className="my-4 overflow-x-auto"><table className="w-full border-collapse text-xs"><thead><tr>{b.headers.map((h,j) => <th key={j} className="border border-line bg-panel px-3 py-2 text-left"><Inline text={h} /></th>)}</tr></thead><tbody>{b.rows.map((row,j) => <tr key={j}>{b.headers.map((_,k) => <td key={k} className="border border-line px-3 py-2"><Inline text={row[k] ?? ""} onReference={onReference} /></td>)}</tr>)}</tbody></table></div>;
          case "p":
            return <p key={i} className="my-2"><Inline text={b.text} onReference={onReference} /></p>;
          case "ul":
          case "ol": {
            const Tag = b.kind;
            return (
              <Tag key={i} className={`my-2 space-y-1 pl-5 ${b.kind === "ul" ? "list-disc" : "list-decimal"}`}>
                {b.items.map((it, j) => <li key={j}><Inline text={it} onReference={onReference} /></li>)}
              </Tag>
            );
          }
          case "code":
            return <CodeBlock key={i} code={b.code} lang={b.lang || "python"} title={heading} />;
        }
      })}
    </div>
  );
}
