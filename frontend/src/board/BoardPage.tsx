import { useState } from "react";
import type { Board, PinState } from "../api";
import { BoardSvg } from "./BoardSvg";
import { ifaceShort } from "./look";
import { PinInspector } from "./PinInspector";

const LEGEND: [string, string, string][] = [
  ["#2a2a2a", "var(--color-pin-free)", "GPIO свободен"],
  ["#1e2c42", "var(--color-pin-out)", "выход"],
  ["#1b302d", "var(--color-pin-in)", "вход"],
  ["var(--color-pin-high)", "var(--color-pin-high)", "HIGH"],
  ["#2c2440", "var(--color-pin-iface)", "интерфейс (uEnv)"],
  ["#203633", "var(--color-pin-adc)", "ADC 0…1.8 В"],
  ["var(--color-pin-5v)", "var(--color-pin-5v)", "+5V"],
  ["var(--color-pin-3v3)", "var(--color-pin-3v3)", "+3V3"],
  ["var(--color-pin-gnd)", "#555", "GND"],
];

interface Props {
  board: Board;
  live: Record<string, PinState>;
  onError: (msg: string) => void;
}

export function BoardPage({ board, live, onError }: Props) {
  const [selected, setSelected] = useState<string | null>("J9-7");
  const [highlight, setHighlight] = useState<Set<string>>(new Set());
  const pin = board.pins.find((p) => p.id === selected);
  const hl = (pins: string[]) => setHighlight(new Set(pins));

  return (
    <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
      <main className="flex min-w-0 flex-1 flex-col gap-5 overflow-auto p-5">
        <div className="rounded-lg border border-line bg-panel p-5">
          <BoardSvg board={board} live={live} selected={selected} highlight={highlight} onSelect={setSelected} />
        </div>

        <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted">
          {LEGEND.map(([fill, stroke, label]) => (
            <span key={label} className="flex items-center gap-1.5">
              <span className="size-3 rounded-full border-[1.5px]" style={{ background: fill, borderColor: stroke }} />
              {label}
            </span>
          ))}
        </div>

        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Интерфейсы (overlays в uEnv.txt)</h3>
          <div className="flex flex-wrap gap-1.5">
            {board.interfaces.map((i) => (
              <button key={i.id}
                      onMouseEnter={() => hl(Object.keys(i.pins))} onMouseLeave={() => hl([])}
                      onClick={() => setSelected(Object.keys(i.pins)[0])}
                      title={`${i.title}: ${Object.entries(i.pins).map(([p, r]) => `${p} ${r}`).join(", ")}`}
                      className={`rounded border px-2 py-1 font-mono text-xs ${i.enabled
                        ? "border-pin-iface/60 bg-pin-iface/15 text-[#cbb8f0]"
                        : "border-line text-muted hover:text-fg"}`}>
                {ifaceShort(i)}
              </button>
            ))}
          </div>
        </section>
      </main>

      <aside className="w-full shrink-0 overflow-auto border-t border-line bg-panel p-5 lg:w-[360px] lg:border-t-0 lg:border-l">
        {pin ? (
          <PinInspector board={board} pin={pin} live={live[pin.id]} onError={onError} onHighlight={hl} />
        ) : (
          <p className="text-muted">Выберите пин на схеме.</p>
        )}
      </aside>
    </div>
  );
}
