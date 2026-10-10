import { Fragment, type ReactNode, useState } from "react";
import { api } from "../api";
import { navigate } from "../route";
import { slug } from "./useHelp";

/** Мини-markdown для docs/sdk: заголовки, абзацы, списки, код, `inline`, **жирный**, ссылки.
 * Без зависимостей — справка должна работать офлайн и весить немного. */
type Block =
  | { kind: "h"; level: number; text: string }
  | { kind: "p"; text: string }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "code"; lang: string; run: boolean; code: string };

export function parseMarkdown(md: string): Block[] {
  const lines = md.replace(/<!--[\s\S]*?-->/g, "").split("\n");
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^```(\w*)\s*(\w+)?\s*$/.exec(line); // ```python run / ```python block
    if (fence) {
      const code: string[] = [];
      for (i++; i < lines.length && !lines[i].startsWith("```"); i++) code.push(lines[i]);
      i++;
      out.push({ kind: "code", lang: fence[1], run: fence[2] === "run", code: code.join("\n") });
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      out.push({ kind: "h", level: h[1].length, text: h[2] });
      i++;
      continue;
    }
    if (/^\s*(-|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*(-|\d+\.)\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*(-|\d+\.)\s+/, "");
        for (i++; i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*(-|\d+\.)\s/.test(lines[i]); i++)
          item += " " + lines[i].trim();
        items.push(item);
      }
      out.push({ kind: ordered ? "ol" : "ul", items });
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*(-|\d+\.)\s)/.test(lines[i])) para.push(lines[i++]);
    out.push({ kind: "p", text: para.join(" ") });
  }
  return out;
}

export function Inline({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    if (m[1] !== undefined) parts.push(<code key={m.index} className="rounded bg-panel-2 px-1 font-mono text-[12px]">{m[1]}</code>);
    else if (m[2] !== undefined) parts.push(<strong key={m.index} className="text-fg">{m[2]}</strong>);
    else parts.push(<a key={m.index} href={m[4]} className="text-accent hover:underline">{m[3]}</a>);
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts.map((p, i) => <Fragment key={i}>{p}</Fragment>)}</>;
}

export function CodeBlock({ code, lang = "python", title }: { code: string; lang?: string; title?: string }) {
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const runnable = lang === "python" && /from pioneer\.sdk import/.test(code);
  const open = () => {
    setBusy(true);
    api.scriptCreate(title ? `Пример: ${title}` : "Пример из справки", code + "\n")
      .then((s) => navigate({ page: "scripts", id: s.id }))
      .finally(() => setBusy(false));
  };
  return (
    <div className="group relative my-3 rounded border border-line bg-[#191919]">
      <pre className="overflow-x-auto p-3 font-mono text-[12px] leading-relaxed">{code}</pre>
      <div className="absolute top-1.5 right-1.5 flex gap-1 opacity-0 transition group-hover:opacity-100">
        <button className="rounded bg-panel-2 px-2 py-0.5 text-[11px] text-muted hover:text-fg"
                onClick={() => navigator.clipboard?.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); })}>
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

export function Markdown({ source }: { source: string }) {
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
            return <Tag key={i} id={slug(b.text)} className={`scroll-mt-4 ${cls}`}><Inline text={b.text} /></Tag>;
          }
          case "p":
            return <p key={i} className="my-2"><Inline text={b.text} /></p>;
          case "ul":
          case "ol": {
            const Tag = b.kind;
            return (
              <Tag key={i} className={`my-2 space-y-1 pl-5 ${b.kind === "ul" ? "list-disc" : "list-decimal"}`}>
                {b.items.map((it, j) => <li key={j}><Inline text={it} /></li>)}
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
