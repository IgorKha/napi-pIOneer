import * as monaco from "monaco-editor/editor/editor.api.js";
import "monaco-editor/languages/definitions/python/register.js";
// editor.api — голое API без фич; выборочно в 0.57 не собрать (подсказки, например, приходят
// через inlineCompletions) — подключаем все фичи редактора, но только язык Python
import "monaco-editor/features/register.all.js";
import EditorWorker from "monaco-editor/editor/editor.worker.start.js?worker";
import {languageProviders, type Scope} from "./language";
import { useEffect, useRef } from "react";
import type { Board, HelpReference, SdkDoc, SdkMember, SdkSymbol } from "../api";
import { loadHelp } from "../help/useHelp";

// Monaco собран в бандл (без CDN): у платы может не быть интернета.
self.MonacoEnvironment = { getWorker: () => new EditorWorker() };

monaco.editor.defineTheme("pioneer-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#1d1d1d",
    "editor.lineHighlightBackground": "#262626",
    "editorLineNumber.foreground": "#5a5a5a",
    "editorGutter.background": "#1d1d1d",
  },
});

// ---- подсказки: SDK (из справочника, сгенерированного из кода — docs/ideas/004) и пины платы
let currentBoard: Board | null = null;
let reference: HelpReference | null = null;
loadHelp().then((r) => (reference = r), () => {});

const PIN_CTOR = /\b(Output|Input|Pwm|Adc)\(\s*["']([^"']*)$/;
const BUS_CTOR = /\b(I2C|SPI|UART|HardwarePwm)\(\s*["']([^"']*)$/;
const BUS_KIND: Record<string, string> = { I2C: "i2c", SPI: "spi", UART: "uart", HardwarePwm: "pwm" };

function docMarkdown(d: SdkDoc & { signature: string }, name?: string): monaco.IMarkdownString {
  const parts = ["```python\n" + (name ? `${name}.` : "") + d.signature + "\n```", d.summary];
  const args = Object.entries(d.args);
  if (args.length) parts.push(args.map(([k, v]) => `- \`${k}\` — ${v}`).join("\n"));
  if (d.returns) parts.push(`**Возвращает:** ${d.returns}`);
  if (d.hardware) parts.push(`⚠ **Железо:** ${d.hardware}`);
  if (d.example) parts.push("```python\n" + d.example + "\n```");
  return { value: parts.join("\n\n") };
}

function findDoc(word: string): { d: SdkSymbol | SdkMember; owner?: string } | null {
  if (!reference) return null;
  const sym = reference.sdk.find((s) => s.name === word);
  if (sym) return { d: sym };
  for (const s of reference.sdk) {
    const m = s.members.find((x) => x.name === word);
    if (m) return { d: m, owner: s.name };
  }
  return null;
}

monaco.languages.registerCompletionItemProvider("python", {
  triggerCharacters: ['"', "'"],
  provideCompletionItems(model, position) {
    const before = model.getValueInRange({
      startLineNumber: position.lineNumber, startColumn: 1,
      endLineNumber: position.lineNumber, endColumn: position.column,
    });
    const word = model.getWordUntilPosition(position);
    const pin = PIN_CTOR.exec(before);
    if (pin && currentBoard) {
      const adc = pin[1] === "Adc";
      const typed = pin[2];
      const range = new monaco.Range(position.lineNumber, position.column - typed.length,
                                     position.lineNumber, position.column);
      return {
        suggestions: currentBoard.pins
          .filter((p) => (adc ? p.kind === "adc" : p.kind === "gpio"))
          .map((p) => {
            const busy = p.claim ? `занят: ${Object.values(p.claim.owners).join(", ")}` : "";
            const blocked = p.runtime_modes.length === 0 ? `недоступен: ${p.function}` : "";
            return {
              label: { label: p.id, description: `${p.name ?? ""} ${blocked || busy}`.trim() },
              kind: monaco.languages.CompletionItemKind.Value,
              insertText: p.id,
              range,
              // свободные → занятые → недоступные; внутри — J9 раньше J10, пины по номеру
              sortText: (blocked ? "2" : busy ? "1" : "0") + p.id.replace(/\d+/g, (n) => n.padStart(2, "0")),
            };
          }),
      };
    }
    const bus = BUS_CTOR.exec(before);
    if (bus && currentBoard) {
      const typed = bus[2];
      const range = new monaco.Range(position.lineNumber, position.column - typed.length,
                                     position.lineNumber, position.column);
      const seen = new Set<string>();
      return {
        suggestions: currentBoard.interfaces
          .filter((i) => i.kind === BUS_KIND[bus[1]] && !seen.has(i.controller) && seen.add(i.controller))
          .map((i) => {
            const variants = currentBoard!.interfaces.filter((v) => v.controller === i.controller);
            const on = variants.find((v) => v.enabled);
            return {
              label: { label: i.controller,
                       description: i.locked ? "системная консоль" : on ? Object.entries(on.pins).map(([p, r]) => `${p} ${r}`).join(", ") : "выключен (нужен оверлей)" },
              kind: monaco.languages.CompletionItemKind.Value,
              insertText: i.controller,
              range,
              sortText: (on && !i.locked ? "0" : "1") + i.controller,
            };
          }),
      };
    }
    const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
    return {
      suggestions: (reference?.sdk ?? []).filter((s) => s.kind !== "exception" || s.name === "PioneerError").map((s) => ({
        label: s.name,
        kind: s.kind === "class" ? monaco.languages.CompletionItemKind.Class : monaco.languages.CompletionItemKind.Function,
        insertText: s.name === "every" ? "@every(${1:1.0})\ndef ${2:tick}():\n    ${3:pass}" : s.snippet ?? s.name,
        insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
        documentation: docMarkdown(s),
        detail: s.summary,
        range,
      })),
    };
  },
});

monaco.languages.registerHoverProvider("python", {
  provideHover(model, position) {
    const w = model.getWordAtPosition(position);
    const found = w && findDoc(w.word);
    if (!w || !found) return null;
    return {
      range: new monaco.Range(position.lineNumber, w.startColumn, position.lineNumber, w.endColumn),
      contents: [docMarkdown(found.d, found.owner)],
    };
  },
});

monaco.languages.registerSignatureHelpProvider("python", {
  signatureHelpTriggerCharacters: ["(", ","],
  signatureHelpRetriggerCharacters: [","],
  provideSignatureHelp(model, position) {
    const before = model.getValueInRange({
      startLineNumber: Math.max(1, position.lineNumber - 5), startColumn: 1,
      endLineNumber: position.lineNumber, endColumn: position.column,
    });
    // ближайшая незакрытая скобка и имя перед ней
    let depth = 0;
    let i = before.length - 1;
    let commas = 0;
    for (; i >= 0; i--) {
      const c = before[i];
      if (c === ")") depth++;
      else if (c === "(") {
        if (depth === 0) break;
        depth--;
      } else if (c === "," && depth === 0) commas++;
    }
    if (i < 0) return null;
    const name = /(\w+)\s*$/.exec(before.slice(0, i))?.[1];
    const found = name && findDoc(name);
    if (!found) return null;
    const d = found.d;
    return {
      value: {
        signatures: [{
          label: d.signature,
          documentation: d.summary,
          parameters: d.params.map((p) => ({ label: p.name, documentation: d.args[p.name] ?? "" })),
        }],
        activeSignature: 0,
        activeParameter: Math.min(commas, Math.max(0, d.params.length - 1)),
      },
      dispose() {},
    };
  },
});

interface Props {
  value: string;
  analysis?: Scope;
  onDefinition?: (location:{path:string;line:number}) => void;
  language?: string;
  line?: number;
  board: Board | null;
  onChange: (v: string) => void;
  onSave: () => void;
}

export function CodeEditor({ value, board, onChange, onSave, language = "python", line, analysis, onDefinition }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const handlers = useRef({ onChange, onSave });
  handlers.current = { onChange, onSave };
  currentBoard = board;

  useEffect(() => {
    const ed = monaco.editor.create(host.current!, {
      model: monaco.editor.createModel(value, language, analysis ? monaco.Uri.parse(`pioneer://code/${analysis.kind}/${analysis.id}/${analysis.path}`) : undefined),
      theme: "pioneer-dark",
      automaticLayout: true,
      fontSize: 13,
      fontFamily: "JetBrains Mono, SF Mono, Menlo, monospace",
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      tabSize: 4,
      insertSpaces: true,
      renderWhitespace: "boundary",
      quickSuggestions: { other: true, strings: true, comments: false },
      // классический ввод через textarea: новый EditContext API пропускает триггеры набора
      // (автозакрытие, подсказки) при вставке текста — надёжнее старый путь
      editContext: false,
    });
    ed.onDidChangeModelContent(() => handlers.current.onChange(ed.getValue()));
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => handlers.current.onSave());
    editor.current = ed;
    if (import.meta.env.DEV) (window as unknown as { __editor: unknown }).__editor = ed; // отладка/проверки
    const cleanupAnalysis = analysis && language === "python" ? languageProviders(ed.getModel()!, analysis) : undefined;
    const opener = analysis ? monaco.editor.registerEditorOpener({openCodeEditor(_source, resource, selection) {
      const prefix = `/${analysis.kind}/${analysis.id}/`;
      if (resource.scheme !== 'pioneer' || !resource.path.startsWith(prefix)) return false;
      const line = selection && ('startLineNumber' in selection ? selection.startLineNumber : selection.lineNumber);
      onDefinition?.({path:resource.path.slice(prefix.length),line:line || 1});
      return true;
    }}) : undefined;
    return () => {cleanupAnalysis?.(); opener?.dispose(); const model=ed.getModel();ed.dispose();model?.dispose();};
    // value — только начальное; смену скрипта делаем через key у компонента
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (line && editor.current) {
      editor.current.revealLineInCenter(line);
      editor.current.setPosition({ lineNumber: line, column: 1 });
      editor.current.focus();
    }
  }, [line]);

  return <div ref={host} className="h-full min-h-0 w-full" />;
}
