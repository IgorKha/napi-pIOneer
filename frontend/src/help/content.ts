export type Block =
  | { kind: "h"; level: number; text: string }
  | { kind: "p" | "quote"; text: string }
  | { kind: "table"; headers: string[]; rows: string[][] }
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
    if (line.trim().startsWith("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] ?? "")) {
      const cells = (row: string) => row.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, "|"));
      const headers = cells(line), rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      out.push({kind: "table", headers, rows});
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (i < lines.length && /^>/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
      out.push({kind: "quote", text: quote.join(" ")});
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
    const para: string[] = [lines[i++]];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|>|\s*\||\s*(-|\d+\.)\s)/.test(lines[i])) para.push(lines[i++]);
    out.push({ kind: "p", text: para.join(" ") });
  }
  return out;
}


/** Local guide links must stay inside the SPA; arbitrary URL schemes are never rendered. */
export function guideLink(url: string, section?: string): string {
  const file = /^(?:\.\/)?([a-z0-9_-]+)\.md(?:#(.+))?$/i.exec(url);
  if (file) {
    let anchor = file[2];
    if (anchor) { try { anchor = decodeURIComponent(anchor); } catch { /* Keep malformed URL text. */ } }
    return `#/help/${file[1]}${anchor ? "/" + encodeURIComponent(anchor) : ""}`;
  }
  if (section && url.startsWith("#") && !url.startsWith("#/")) return `#/help/${section}/${encodeURIComponent(url.slice(1))}`;
  return url;
}
