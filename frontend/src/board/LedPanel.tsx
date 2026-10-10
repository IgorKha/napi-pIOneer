import { api, type BoardLed, type LedState } from "../api";
import { Owners } from "./PinInspector";

const MANUAL = "manual";
const COMMON_TRIGGERS = ["heartbeat", "timer", "default-on", "mmc0", "mmc1", "none"];

/** Встроенные LED (gpio-leds): живое состояние, вкл/выкл, триггер ядра, возврат системного. */
export function LedPanel({ leds, live, run }: {
  leds: BoardLed[]; live: Record<string, LedState>; run: (p: Promise<unknown>) => void;
}) {
  if (leds.length === 0) return null;
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Светодиоды на плате</h3>
      <div className="flex flex-wrap gap-3">
        {leds.map((led) => {
          const st = { ...led, ...live[led.id] };
          const owners = st.claim ? Object.keys(st.claim.owners) : [];
          const foreign = owners.some((o) => o !== MANUAL);
          const triggers = COMMON_TRIGGERS.filter((t) => led.triggers.includes(t));
          return (
            <div key={led.id} className="flex items-center gap-2 rounded border border-line bg-panel px-3 py-2">
              <span aria-hidden className="size-3.5 shrink-0 rounded-full border border-line transition-[background,box-shadow] duration-75"
                    style={{ background: st.on ? led.color : "#2a2a2a",
                             boxShadow: st.on ? `0 0 8px ${led.color}` : undefined }} />
              <span className="w-28 shrink-0 text-xs">{led.title}</span>
              {foreign ? (
                <span className="w-48 text-xs"><Owners owners={st.claim!.owners} /></span>
              ) : (
                <>
                  <div className="flex gap-1">
                    {(["off", "on"] as const).map((a) => (
                      <button key={a} onClick={() => run(api.led(led.id, a))}
                              className={`w-10 rounded py-1 text-xs ${st.trigger === "none" && st.on === (a === "on")
                                ? "bg-accent text-white" : "bg-panel-2 hover:bg-[#3a3a3a]"}`}>
                        {a === "on" ? "вкл" : "выкл"}
                      </button>
                    ))}
                  </div>
                  <select aria-label={`Триггер: ${led.title}`} value={st.trigger}
                          onChange={(e) => run(api.led(led.id, "trigger", e.target.value))}
                          className="w-28 rounded border border-line bg-bg px-1.5 py-1 text-xs">
                    {[...new Set([st.trigger, ...triggers])].map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                  <button onClick={() => run(api.led(led.id, "release"))} disabled={!st.claim}
                          title={`Вернуть системный триггер (${led.default_trigger})`}
                          className="text-xs text-accent hover:underline disabled:invisible">
                    сброс
                  </button>
                </>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
