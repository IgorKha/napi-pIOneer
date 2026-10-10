import { useState } from "react";
import { api, type Board, type BenchModel, type Pin } from "../api";

/** Модели стенда на пине: живые контролы + добавление. Только при hal = bench. */

export function modelPins(m: BenchModel): string[] {
  const c = m.config;
  return [c.pin, c.a, c.b].filter((p): p is string => typeof p === "string");
}

interface Props {
  board: Board;
  pin: Pin;
  models: BenchModel[];
  run: (p: Promise<unknown>) => void;
}

export function BenchSection({ board, pin, models, run }: Props) {
  const attached = models.filter((m) => modelPins(m).includes(pin.id));
  const isAdc = pin.kind === "adc";
  const add = (kind: string, config: Record<string, unknown>) => run(api.benchAdd(kind, { pin: pin.id, ...config }));

  return (
    <section className="rounded border border-dashed border-warn/50 p-3">
      <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-warn">
        Стенд
        <span className="font-normal normal-case tracking-normal text-muted">— виртуальные устройства на пине</span>
      </h3>

      {attached.length > 0 && (
        <ul className="mb-3 flex flex-col gap-2">
          {attached.map((m) => (
            <li key={m.id} className="flex items-center gap-2">
              <span className="w-28 shrink-0 text-xs text-muted">{label(m, pin.id)}</span>
              <div className="flex flex-1 items-center gap-1.5">
                <ModelControl board={board} model={m} run={run} />
              </div>
              <button aria-label={`Убрать ${m.title}`} title="Убрать со стенда"
                      className="px-1 text-muted hover:text-err" onClick={() => run(api.benchRemove(m.id))}>×</button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-1.5">
        {isAdc ? (
          <AddBtn onClick={() => add("voltage", { volts: 0.9 })}>+ источник напряжения</AddBtn>
        ) : (
          <>
            <AddBtn onClick={() => add("button", {})}>+ кнопка</AddBtn>
            <AddBtn onClick={() => add("led", {})}>+ LED</AddBtn>
            <AddBtn onClick={() => add("level", {})}>+ внешний уровень</AddBtn>
            <WireAdd board={board} pin={pin} run={run} />
          </>
        )}
      </div>
    </section>
  );
}

function label(m: BenchModel, pinId: string): string {
  if (m.kind === "wire") {
    const other = m.config.a === pinId ? m.config.b : m.config.a;
    return `Перемычка ↔ ${other}`;
  }
  return m.title;
}

function ModelControl({ board, model: m, run }: { board: Board; model: BenchModel; run: (p: Promise<unknown>) => void }) {
  const act = (name: string, payload: Record<string, unknown> = {}) => run(api.benchAction(m.id, name, payload));
  const s = m.state;
  switch (m.kind) {
    case "button":
      return (
        <>
          <button
            className={`rounded px-3 py-1 text-xs select-none ${s.pressed ? "bg-warn text-bg" : "bg-panel-2 hover:bg-[#3a3a3a]"}`}
            onPointerDown={() => act("press")} onPointerUp={() => act("release")}
            onPointerLeave={() => s.pressed && act("release")}
            onKeyDown={(e) => { if (e.key === " " && !e.repeat) { e.preventDefault(); act("press"); } }}
            onKeyUp={(e) => { if (e.key === " ") act("release"); }}>
            {s.pressed ? "нажата" : "зажать"}
          </button>
          <button className="text-xs text-accent hover:underline" onClick={() => act("click")}>клик</button>
          {typeof s.warning === "string" && <span className="text-xs text-warn" title={s.warning}>⚠</span>}
        </>
      );
    case "led": {
      const b = Number(s.brightness ?? 0);
      const color = String(m.config.color ?? "red");
      return (
        <>
          <span className="size-4 rounded-full border border-line"
                style={{ background: b > 0 ? color : "#2a2a2a", opacity: b > 0 ? 0.35 + 0.65 * b : 1,
                         boxShadow: b > 0 ? `0 0 ${6 + 8 * b}px ${color}` : undefined }} />
          <span className="text-xs text-muted">
            {b > 0 ? (b < 1 ? `${Math.round(b * 100)}%` : "горит") : "погашен"}
          </span>
        </>
      );
    }
    case "level":
      return (
        <div className="flex gap-1">
          {[false, true].map((v) => (
            <button key={String(v)} onClick={() => act("set", { value: v })}
                    className={`rounded px-2.5 py-1 text-xs ${s.value === v ? "bg-accent text-white" : "bg-panel-2 hover:bg-[#3a3a3a]"}`}>
              {v ? "1" : "0"}
            </button>
          ))}
        </div>
      );
    case "voltage": {
      const v = Number(s.volts ?? 0);
      return (
        <>
          <input type="range" min={0} max={3.3} step={0.01} value={v} className="flex-1 accent-pin-adc"
                 aria-label="Напряжение источника"
                 onChange={(e) => act("set", { volts: Number(e.target.value) })} />
          <span className={`w-14 text-right font-mono text-xs ${v > board.adc.vref ? "text-err" : ""}`}
                title={v > board.adc.vref ? `больше ${board.adc.vref} В — нужен делитель` : undefined}>
            {v.toFixed(2)} В
          </span>
        </>
      );
    }
    case "wire":
      return <span className="font-mono text-xs">{s.level ? "HIGH" : "LOW"}</span>;
    default:
      return <span className="font-mono text-xs text-muted">{JSON.stringify(s)}</span>;
  }
}

function WireAdd({ board, pin, run }: { board: Board; pin: Pin; run: (p: Promise<unknown>) => void }) {
  const [open, setOpen] = useState(false);
  if (!open) return <AddBtn onClick={() => setOpen(true)}>+ перемычка…</AddBtn>;
  const targets = board.pins.filter((p) => p.gpio && p.id !== pin.id);
  return (
    <select autoFocus aria-label="Второй конец перемычки" defaultValue=""
            className="rounded border border-line bg-bg px-2 py-1 text-xs"
            onBlur={() => setOpen(false)}
            onChange={(e) => { setOpen(false); run(api.benchAdd("wire", { a: pin.id, b: e.target.value })); }}>
      <option value="" disabled>к пину…</option>
      {targets.map((p) => <option key={p.id} value={p.id}>{p.id} · {p.name}</option>)}
    </select>
  );
}

function AddBtn({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick}
            className="rounded border border-line px-2 py-1 text-xs text-muted hover:border-warn/60 hover:text-fg">
      {children}
    </button>
  );
}
