import * as monaco from "monaco-editor/editor/editor.api.js";
import "monaco-editor/languages/definitions/python/register.js";
// editor.api — голое API без фич; выборочно в 0.57 не собрать (подсказки, например, приходят
// через inlineCompletions) — подключаем все фичи редактора, но только язык Python
import "monaco-editor/features/register.all.js";
import EditorWorker from "monaco-editor/editor/editor.worker.start.js?worker";
import { useEffect, useRef } from "react";
import type { Board } from "../api";

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

// ---- подсказки: SDK и пины платы (доска подставляется при монтировании редактора)
let currentBoard: Board | null = null;

const SDK: [string, string, string][] = [
  ["Output", 'Output("${1:J9-7}")', "Цифровой выход: .on() .off() .toggle() .value"],
  ["Input", 'Input("${1:J10-2}", debounce_ms=${2:30})', "Цифровой вход: @x.on_press / on_rise / on_fall / on_change"],
  ["Pwm", 'Pwm("${1:J9-12}", freq=${2:100}, duty=${3:0.5})', "Soft-PWM ≤ 500 Гц, .duty 0…1"],
  ["Adc", 'Adc("${1:J9-26}")', "АЦП 0…1.8 В: .volts"],
  ["BoardLed", 'BoardLed("${1|user,power|}")', "Встроенный LED: .on() .off() .trigger('heartbeat')"],
  ["I2C", 'I2C("${1:i2c1}", 0x${2:3C})', "Устройство I2C (интерфейс, адрес): read_reg / write_reg / write_read"],
  ["UART", 'UART("${1:uart3}", baud=${2:9600})', "Порт (интерфейс): .write(b) .read(n, timeout)"],
  ["SPI", 'SPI("${1:spi2}", cs=${2:0})', "SPI (интерфейс, CS): .xfer(bytes)"],
  ["every", "@every(${1:1.0})\ndef ${2:tick}():\n    ${3:pass}", "Вызывать функцию каждые N секунд"],
  ["after", "after(${1:1.0}, ${2:fn})", "Однократно через N секунд"],
  ["log", "log(${1})", "Печать в лог скрипта"],
];

const PIN_CTOR = /\b(Output|Input|Pwm|Adc)\(\s*["']([^"']*)$/;

monaco.languages.registerCompletionItemProvider("python", {
  triggerCharacters: ['"', "'"],
  provideCompletionItems(model, position) {
    const before = model.getValueInRange({
      startLineNumber: position.lineNumber, startColumn: 1,
      endLineNumber: position.lineNumber, endColumn: position.column,
    });
    const word = model.getWordUntilPosition(position);
    const m = PIN_CTOR.exec(before);
    if (m && currentBoard) {
      const adc = m[1] === "Adc";
      const typed = m[2];
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
    const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
    return {
      suggestions: SDK.map(([label, snippet, doc]) => ({
        label,
        kind: monaco.languages.CompletionItemKind.Function,
        insertText: snippet,
        insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
        documentation: doc,
        detail: "pioneer.sdk",
        range,
      })),
    };
  },
});

interface Props {
  value: string;
  board: Board | null;
  onChange: (v: string) => void;
  onSave: () => void;
}

export function CodeEditor({ value, board, onChange, onSave }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const handlers = useRef({ onChange, onSave });
  handlers.current = { onChange, onSave };
  currentBoard = board;

  useEffect(() => {
    const ed = monaco.editor.create(host.current!, {
      value,
      language: "python",
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
    return () => ed.dispose();
    // value — только начальное; смену скрипта делаем через key у компонента
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return <div ref={host} className="h-full min-h-0 w-full" />;
}
