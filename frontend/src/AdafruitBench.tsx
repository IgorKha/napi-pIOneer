import { useEffect, useState } from "react";
import { api, type BenchModel } from "./api";

export function AdafruitBench({
  onError,
}: {
  onError: (message: string) => void;
}) {
  const [models, setModels] = useState<BenchModel[]>([]);
  const [kind, setKind] = useState("bme280");
  const [bus, setBus] = useState(1);
  const [addr, setAddr] = useState("0x76");
  const reload = () =>
    api.bench().then(
      (r) => setModels(r.models),
      (e: Error) => onError(e.message),
    );
  useEffect(() => {
    reload();
    const timer = setInterval(reload, 2000);
    return () => clearInterval(timer);
  }, []);
  const run = async (p: Promise<unknown>) => {
    try {
      await p;
      await reload();
    } catch (e) {
      onError((e as Error).message);
    }
  };
  return (
    <section className="my-5 rounded border border-warn/40 p-3">
      <h3 className="font-semibold text-warn">Протокольные модели стенда</h3>
      <p className="my-2 text-xs text-muted">
        Скрипт и библиотека не меняются: модели отвечают на реальные
        I²C-транзакции. Для остальных чипов модели нет.
      </p>
      <div className="flex flex-wrap gap-3">
        <select
          aria-label="Модель стенда"
          className="bg-bg"
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setAddr(
              {
                bme280: "0x76",
                sht4x: "0x44",
                ssd1306: "0x3c",
                pca9685: "0x40",
              }[e.target.value] || "0x76",
            );
          }}
        >
          {["bme280", "sht4x", "ssd1306", "pca9685"].map((k) => (
            <option key={k}>{k}</option>
          ))}
        </select>
        <label>
          I²C bus{" "}
          <input
            aria-label="Номер шины стенда"
            className="w-14 bg-bg"
            type="number"
            min={0}
            max={3}
            value={bus}
            onChange={(e) => setBus(Number(e.target.value))}
          />
        </label>
        <input
          aria-label="Адрес модели"
          className="w-20 bg-bg"
          value={addr}
          onChange={(e) => setAddr(e.target.value)}
        />
        <button
          onClick={() => run(api.benchAdd(kind, { bus, addr: Number(addr) }))}
        >
          Добавить модель
        </button>
      </div>
      {models
        .filter((m) =>
          ["bme280", "sht4x", "ssd1306", "pca9685"].includes(m.kind),
        )
        .map((m) => (
          <div className="mt-3 border-t border-line pt-3" key={m.id}>
            <div className="flex justify-between">
              <span>
                {m.title} · I²C{String(m.config.bus)}{" "}
                {"0x" + Number(m.config.addr).toString(16)}
              </span>
              <button
                aria-label={`Удалить модель ${m.title}`}
                onClick={() => run(api.benchRemove(m.id))}
              >
                ×
              </button>
            </div>
            {["temperature", "humidity", "pressure"]
              .filter((p) => p in m.state)
              .map((p) => (
                <label key={p} className="my-2 flex items-center gap-3 text-xs">
                  <span className="w-24">{p}</span>
                  <input
                    aria-label={`${m.title} ${p}`}
                    className="flex-1"
                    type="range"
                    min={p === "temperature" ? -40 : p === "humidity" ? 0 : 300}
                    max={
                      p === "temperature" ? 85 : p === "humidity" ? 100 : 1100
                    }
                    step={0.1}
                    value={Number(m.state[p])}
                    onChange={(e) =>
                      run(
                        api.benchAction(m.id, "set", {
                          [p]: Number(e.target.value),
                        }),
                      )
                    }
                  />
                  <span>{String(m.state[p])}</span>
                </label>
              ))}
            {m.kind === "ssd1306" && <OledFrame model={m} />}
            {m.kind === "pca9685" && (
              <pre className="overflow-auto text-xs">
                {JSON.stringify(m.state, null, 2)}
              </pre>
            )}
          </div>
        ))}
    </section>
  );
}

function OledFrame({ model }: { model: BenchModel }) {
  const width = Number(model.state.width),
    height = Number(model.state.height),
    hex = String(model.state.frame || "");
  let path = "";
  for (let index = 0; index < hex.length / 2; index++) {
    const bits = parseInt(hex.slice(index * 2, index * 2 + 2), 16);
    for (let bit = 0; bit < 8; bit++)
      if (bits & (1 << bit))
        path += `M${index % width},${Math.floor(index / width) * 8 + bit}h1v1h-1z`;
  }
  return (
    <svg
      aria-label="Кадр OLED на стенде"
      role="img"
      viewBox={`0 0 ${width} ${height}`}
      className="my-2 w-64 bg-black"
      style={{ imageRendering: "pixelated" }}
    >
      <path d={path} fill="white" />
    </svg>
  );
}
