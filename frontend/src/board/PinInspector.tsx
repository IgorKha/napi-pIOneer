import { useEffect, useState } from "react";
import { api, type BenchModel, type Board, type InterfacePlan, type Pin, type PinState, type RuntimeMode } from "../api";
import { href, ownerRoute } from "../route";
import { Tip } from "../Tip";
import { BenchSection } from "./BenchSection";
import { formatValue, ifaceShort } from "./look";

const MODE_LABEL: Record<RuntimeMode, string> = {
  gpio_in: "Вход",
  gpio_out: "Выход",
  soft_pwm: "Soft-PWM",
  adc: "ADC",
};
const PULL_LABEL = { up: "pull-up", down: "pull-down", none: "нет" } as const;
const MANUAL = "manual";

interface Props {
  board: Board;
  pin: Pin;
  live: PinState | undefined;
  bench: BenchModel[];
  onError: (msg: string) => void;
  onHighlight: (pins: string[]) => void;
}

const SDK_SNIPPET: Record<string, (c: string) => string> = {
  uart: (c) => `UART("${c}", baud=9600)`,
  i2c: (c) => `I2C("${c}", 0x3C)`,
  spi: (c) => `SPI("${c}", cs=0)`,
};

export function PinInspector({ board, pin, live, bench, onError, onHighlight }: Props) {
  const st: PinState = { value: live?.value ?? pin.value, claim: live ? live.claim : pin.claim };
  const iface = board.interfaces.find((i) => i.id === pin.function);
  const run = (p: Promise<unknown>) => p.catch((e: Error) => onError(e.message));

  return (
    <div className="flex flex-col gap-4">
      <header>
        <div className="font-mono text-base font-semibold">{pin.id}</div>
        <div className="text-muted">
          {pin.name ?? pin.special ?? pin.power?.toUpperCase()}
          {pin.gpio && <span className="font-mono"> · gpio{pin.gpio.chip}:{pin.gpio.line}</span>}
        </div>
      </header>

      {pin.kind === "power" || pin.kind === "special" ? (
        <Section title="Пин">
          <p className="text-muted">
            {pin.power === "gnd" ? "Общий провод." : pin.power ? `Питание ${pin.power === "5v" ? "+5 В" : "+3.3 В"}.` : "Служебный пин, не управляется."}
          </p>
        </Section>
      ) : (
        <>
          <Section title="Функция (pinmux)">
            {pin.function === "kernel" ? (
              <>
                <Badge color="kernel">Драйвер ядра</Badge>
                <p className="mt-2 text-muted">
                  Пин держит ядро: <span className="font-mono text-fg">{pin.kernel_users.join(", ")}</span>.
                  Так бывает с user_overlays и драйверами вроде rts-gpios. Как GPIO недоступен, пока узел включён в Device Tree.
                </p>
              </>
            ) : iface ? (
              <>
                <Badge color="iface">{iface.title} · {iface.pins[pin.id]}</Badge>
                <p className="mt-2 text-muted">
                  Пин отдан интерфейсу оверлеем из uEnv.txt{iface.device && <> ({iface.device})</>}.
                  {iface.locked ? " Это системная консоль — её нельзя выключить." : " Как GPIO недоступен до выключения интерфейса и перезагрузки."}
                </p>
                {iface.note && <p className="mt-1 text-warn">{iface.note}</p>}
                {iface.pending && (
                  <p className="mt-1 text-warn">В uEnv.txt он уже {iface.enabled_in_uenv ? "включён" : "выключен"} — изменится после перезагрузки.</p>
                )}
                {iface.holders.length > 0 && (
                  <p className="mt-1 text-warn">Порт {iface.device} открыт: {iface.holders.join(", ")}.</p>
                )}
                {iface.enabled && !iface.locked && SDK_SNIPPET[iface.kind] && (
                  <p className="mt-1 text-muted">
                    В скрипте: <span className="font-mono text-fg">{SDK_SNIPPET[iface.kind](iface.controller)}</span>
                  </p>
                )}
              </>
            ) : (
              <Badge color={pin.kind === "adc" ? "adc" : "free"}>{pin.kind === "adc" ? "ADC" : "GPIO"}</Badge>
            )}
          </Section>

          {pin.runtime_modes.length > 0 && <Control pin={pin} st={st} run={run} />}

          {board.hal === "bench" && pin.runtime_modes.length > 0 && (
            <BenchSection board={board} pin={pin} models={bench} run={run} />
          )}

          <Section title="Свойства">
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              {pin.default_pull && (<><dt className="text-muted">Подтяжка</dt>
                <dd>{PULL_LABEL[pin.default_pull]} <span className="text-muted">— по умолчанию; изменение через DT и перезагрузку</span></dd></>)}
              {pin.drive_ma && (<><dt className="text-muted">Ток выхода</dt><dd>{pin.drive_ma} мА</dd></>)}
              {pin.alt.length > 0 && (<><dt className="text-muted">Альтернативы</dt><dd className="font-mono">{pin.alt.join(", ")}</dd></>)}
            </dl>
          </Section>

          {pin.interface_options.length > 0 && (
            <OtherFunctions board={board} pin={pin} onError={onError} onHighlight={onHighlight} />
          )}

          <Warnings pin={pin} />
        </>
      )}
    </div>
  );
}

function Control({ pin, st, run }: { pin: Pin; st: PinState; run: (p: Promise<unknown>) => void }) {
  const claim = st.claim;
  const foreign = claim && Object.keys(claim.owners).some((o) => o !== MANUAL);
  const manualModes = pin.runtime_modes.filter((m): m is "gpio_in" | "gpio_out" | "adc" => m !== "soft_pwm");
  const value = formatValue(st);

  return (
    <Section title="Режим сейчас">
      <div className="mb-3 flex items-center gap-2">
        {claim ? <Badge color={claim.mode === "gpio_out" ? "out" : "in"}>{MODE_LABEL[claim.mode as RuntimeMode]}</Badge>
               : <Badge color="free">Свободен</Badge>}
        {/* владелец — сколько влезет (полностью в подсказке), значение — в слоте фиксированной ширины,
            чтобы строка не прыгала при LOW ↔ HIGH и смене вольт */}
        <span className="min-w-0 flex-1">
          {claim && <Owners owners={claim.owners} />}
        </span>
        <span className="w-[8ch] shrink-0 text-right font-mono text-lg font-semibold tabular-nums text-pin-high">
          {value}
        </span>
      </div>

      {foreign ? (
        <p className="text-muted">Пин держит проект — ручное управление недоступно.</p>
      ) : (
        <>
          <div className="flex flex-wrap gap-1.5">
            {manualModes.map((m) => (
              <Btn key={m} active={claim?.mode === m} onClick={() => run(api.manual(pin.id, m))}>
                {MODE_LABEL[m]}
              </Btn>
            ))}
            {claim && <Btn onClick={() => run(api.manual(pin.id, "release"))}>Освободить</Btn>}
          </div>
          <p className="mt-1.5 text-xs text-muted">
            Переключается на лету, без перезагрузки. Выход при смене режима сбрасывается в LOW.
          </p>

          {claim?.mode === "gpio_out" && (
            <div className="mt-3 flex gap-1.5">
              <Btn active={st.value === false} onClick={() => run(api.manual(pin.id, "gpio_out", false))}>LOW</Btn>
              <Btn active={st.value === true} onClick={() => run(api.manual(pin.id, "gpio_out", true))}>HIGH</Btn>
            </div>
          )}
        </>
      )}

    </Section>
  );
}

function OtherFunctions({ board, pin, onError, onHighlight }: {
  board: Board; pin: Pin; onError: (m: string) => void; onHighlight: (pins: string[]) => void;
}) {
  const [plan, setPlan] = useState<{ target: string; plan: InterfacePlan } | null>(null);
  useEffect(() => setPlan(null), [pin.id]);

  const preview = async (target: string) => {
    const t = board.interfaces.find((i) => i.id === target)!;
    // Включить target: выключить включённые конфликтующие. Выключить: только его.
    const req = t.enabled_in_uenv
      ? api.plan([], [target])
      : api.plan([target], t.conflicts.filter((c) => board.interfaces.find((i) => i.id === c)?.enabled_in_uenv));
    try {
      setPlan({ target, plan: await req });
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <Section title="Другие функции пина">
      <p className="mb-2 text-xs text-muted">Меняются оверлеем в uEnv.txt. Сначала diff и бэкап, перезагрузка — отдельным действием.</p>
      <ul className="flex flex-col gap-1">
        {pin.interface_options.map(({ id, role }) => {
          const i = board.interfaces.find((x) => x.id === id)!;
          return (
            <li key={id} className="flex items-center gap-2 rounded px-2 py-1 hover:bg-panel-2"
                onMouseEnter={() => onHighlight(Object.keys(i.pins))} onMouseLeave={() => onHighlight([])}>
              <span className={`size-2 rounded-full ${i.enabled ? "bg-pin-iface" : "border border-muted"}`} />
              <span className="font-mono">{ifaceShort(i)}</span>
              <span className="text-muted">{role} · {Object.keys(i.pins).join(", ")}</span>
              {!i.locked && (
                <button className="ml-auto text-xs text-accent hover:underline" onClick={() => preview(id)}>
                  {i.enabled_in_uenv ? "выключить?" : "включить?"}
                </button>
              )}
            </li>
          );
        })}
      </ul>
      {plan && <PlanView key={plan.plan.hash ?? plan.target} board={board} target={plan.target} plan={plan.plan} onError={onError} />}
    </Section>
  );
}

function PlanView({ board, target, plan, onError }: { board: Board; target: string; plan: InterfacePlan; onError: (m: string) => void }) {
  const [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false), [backup, setBackup] = useState("");
  const title = (id: string) => board.interfaces.find((i) => i.id === id)?.title ?? id;
  const added = plan.overlays.filter((o) => !board.overlays.includes(o));
  const removed = board.overlays.filter((o) => !plan.overlays.includes(o));
  return (
    <div className="mt-3 rounded border border-line bg-bg p-3 text-xs">
      <div className="mb-2 font-semibold">Что будет: {title(target)}</div>
      {plan.disable.length > 0 && <p>Выключится: {plan.disable.map(title).join(", ")}</p>}
      {plan.enable.length > 0 && <p>Включится: {plan.enable.map(title).join(", ")}</p>}
      {Object.keys(plan.pins_to_interface).length > 0 && (
        <p>Займёт пины: <span className="font-mono">{Object.keys(plan.pins_to_interface).join(", ")}</span></p>
      )}
      {plan.pins_to_gpio.length > 0 && (
        <p>Освободит как GPIO: <span className="font-mono">{plan.pins_to_gpio.join(", ")}</span></p>
      )}
      <div className="mt-2 font-mono">
        {removed.map((o) => <div key={o} className="text-err">− {o}</div>)}
        {added.map((o) => <div key={o} className="text-ok">+ {o}</div>)}
      </div>
      {plan.needs_reboot && <p className="mt-2 text-warn">Нужна перезагрузка платы.</p>}
      {plan.diff && <pre className="my-2 max-h-36 overflow-auto whitespace-pre-wrap text-[10px]">{plan.diff}</pre>}
      {plan.missing?.length ? <p className="text-err">Отсутствуют .dtbo: {plan.missing.join(", ")}</p> : null}
      {Object.keys(plan.claims ?? {}).length > 0 && <p className="text-err">Сначала остановите владельцев: {Object.values(plan.claims ?? {}).flatMap((v) => Object.values(v)).join(", ")}</p>}
      {plan.writable && plan.needs_reboot && !backup && <>
        <p className="mt-2 text-muted">Восстановление при ошибке загрузки: microSD с системой, UART0 1 500 000 бод; крайний вариант — maskrom + rkdeveloptool.</p>
        <label className="my-2 flex gap-2"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />Подтверждаю diff и изменение функций пинов</label>
        <button disabled={!confirmed || busy || !!plan.missing?.length || Object.keys(plan.claims ?? {}).length > 0} className="rounded bg-accent px-2 py-1 text-white disabled:opacity-40" onClick={async () => {
          setBusy(true);
          try {
            const r = await fetch("/pioneer/api/board/interfaces/apply", { method: "POST", headers: { "X-Pioneer": "1", "Content-Type": "application/json" }, body: JSON.stringify({ enable: plan.enable, disable: plan.disable, hash: plan.hash, confirmed }) });
            const result = await r.json(); if (!r.ok) throw new Error(result.detail ?? `HTTP ${r.status}`);
            setBackup(result.backup ?? "Изменений нет");
            window.dispatchEvent(new Event("pioneer-board-changed"));
          } catch (e) { onError((e as Error).message); } finally { setBusy(false); }
        }}>Записать uEnv с бэкапом</button>
      </>}
      {backup && <><p className="mt-2 break-all text-muted">Бэкап: {backup}</p>
        <button className="mt-2 rounded bg-warn px-2 py-1 text-bg" disabled={busy || board.hal !== "linux"} onClick={async () => {
          if (!confirm("Перезагрузить плату? Все проекты и скрипты остановятся; затем включатся новые интерфейсы и автозапуск.")) return;
          setBusy(true);
          try {
            const r = await fetch("/pioneer/api/board/reboot", { method: "POST", headers: { "X-Pioneer": "1", "Content-Type": "application/json" }, body: JSON.stringify({ confirmation: "REBOOT" }) });
            if (!r.ok) throw new Error(`Перезагрузка: HTTP ${r.status}`);
          } catch (e) { onError((e as Error).message); } finally { setBusy(false); }
        }}>Перезагрузить плату</button>
      </>}
    </div>
  );
}

function Warnings({ pin }: { pin: Pin }) {
  const items =
    pin.kind === "adc"
      ? ["Диапазон 0…1.8 В — выше только через делитель.", "10 бит, ≈1.76 мВ/шаг."]
      : ["Логика 3.3 В, 5 В на вход — нельзя.",
         `Ток выхода ${pin.drive_ma ?? 2} мА: LED через резистор, реле — только модулем с драйвером.`];
  return (
    <ul className="flex flex-col gap-1 border-l-2 border-warn/60 pl-3 text-xs text-muted">
      {items.map((t) => <li key={t}>{t}</li>)}
    </ul>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{title}</h3>
      {children}
    </section>
  );
}

const BADGE = {
  kernel: "bg-warn/15 text-[#e8c08a] border-warn/60",
  iface: "bg-pin-iface/20 text-[#cbb8f0] border-pin-iface/60",
  free: "bg-panel-2 text-fg border-line",
  out: "bg-pin-out/20 text-[#9db8e0] border-pin-out/60",
  in: "bg-pin-in/20 text-[#8fd9cf] border-pin-in/60",
  adc: "bg-pin-adc/20 text-[#8fd9cf] border-pin-adc/60",
};

function Badge({ color, children }: { color: keyof typeof BADGE; children: React.ReactNode }) {
  return <span className={`inline-block rounded border px-2 py-0.5 text-xs ${BADGE[color]}`}>{children}</span>;
}

function Btn({ active, onClick, children }: { active?: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick}
            className={`rounded px-3 py-1 text-xs transition-colors ${active
              ? "bg-accent text-white"
              : "bg-panel-2 text-fg hover:bg-[#3a3a3a]"}`}>
      {children}
    </button>
  );
}

/** Владельцы захвата: полное имя — в подсказке; скрипт — ссылка, клик проваливает в него. */
export function Owners({ owners }: { owners: Record<string, string> }) {
  const text = Object.values(owners).join(", ");
  return (
    <Tip text={text} className="block">
      <span className="block truncate text-muted">
        {Object.entries(owners).map(([id, name], i) => {
          const r = ownerRoute(id);
          return (
            <span key={id}>
              {i > 0 && ", "}
              {r ? <a href={href(r)} className="text-fg underline decoration-muted underline-offset-2 hover:decoration-fg">{name}</a>
                 : <span tabIndex={0} className="outline-none focus-visible:text-fg">{name}</span>}
            </span>
          );
        })}
      </span>
    </Tip>
  );
}
