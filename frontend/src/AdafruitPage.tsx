import { useEffect, useState } from "react";
import { driverRequest as request } from "./driverApi";
import { AdafruitBench } from "./AdafruitBench";
import { api, type Board } from "./api";
import { navigate } from "./route";

export type Profile = {
  title: string;
  address: number;
  model: string | null;
  properties: string[];
};
export type Driver = {
  package: string;
  chip: string;
  url: string;
  version?: string;
  requested: boolean;
  compatibility: string;
};
type Job = { state: string; log: string; error?: string; package: string };
export type Catalog = {
  drivers: Driver[];
  profiles: Record<string, Profile>;
  offline: boolean;
  job: Job | null;
};

export async function installDriver(
  packageName: string,
  onJob: (job: Job | null) => void,
  version?: string,
) {
  let result = await request("/adafruit/change", {
    package: packageName,
    version,
  });
  if (result.confirmation_required) {
    if (
      !confirm(
        `Изменить общий драйвер? Используется: ${result.uses.map((i: { name: string }) => i.name).join(", ")}. Уже запущенные процессы сохранят прежнюю версию до перезапуска; lock потребуется обновить через мастер.`,
      )
    )
      return false;
    result = await request("/adafruit/change", {
      package: packageName,
      version,
      confirmed: true,
    });
  }
  let job: Job = result.job;
  onJob(job);
  while (job.state === "running") {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    job = await request("/adafruit/job");
    onJob(job);
  }
  if (job.state !== "done")
    throw new Error(job.error || "Не удалось установить драйвер");
  return true;
}

export function AdafruitPage({
  board,
  onError,
}: {
  board: Board | null;
  onError: (message: string) => void;
}) {
  const [catalog, setCatalog] = useState<Catalog>();
  const [query, setQuery] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [uses, setUses] = useState<
    { name: string; kind: string; id: string }[] | null
  >(null);
  const [wizard, setWizard] = useState(false);
  const reload = async (refresh = false) => {
    const c: Catalog = await request(`/adafruit?refresh=${refresh}`);
    setCatalog(c);
    setJob(c.job);
  };
  useEffect(() => {
    reload().catch((e: Error) => onError(e.message));
  }, []);
  useEffect(() => {
    if (job?.state !== "running") return;
    const timer = setTimeout(
      () =>
        request("/adafruit/job").then(setJob, (e: Error) => onError(e.message)),
      1000,
    );
    return () => clearTimeout(timer);
  }, [job]);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await reload();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="max-w-4xl">
      <h2 className="text-lg font-semibold">Adafruit · CircuitPython</h2>
      <p className="my-2 text-muted">
        Одна версия драйвера на плату. Доступ к устройствам — через захваты
        Pioneer. Изменения не перезапускают ваши проекты.
      </p>
      <div className="my-4 flex gap-3">
        <input
          aria-label="Поиск драйверов"
          className="rounded border border-line bg-bg p-2"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="BME280, OLED, servo…"
        />
        <button disabled={busy} onClick={() => act(() => reload(true))}>
          Обновить каталог
        </button>
        <button onClick={() => setWizard(true)} className="text-accent">
          Подключить устройство / пример
        </button>
      </div>
      {catalog?.offline && (
        <p className="text-warn">
          Каталог недоступен: показаны установленные драйверы.
        </p>
      )}
      {job && (
        <div role="status" className="my-3 rounded border border-line p-3">
          <p>
            {job.package}: {job.state}
            {job.error && ` · ${job.error}`}
          </p>
          <pre className="max-h-52 overflow-auto whitespace-pre-wrap text-xs">
            {job.log}
          </pre>
        </div>
      )}
      {uses && (
        <div className="my-3 border border-line p-3">
          <h3>Где используется</h3>
          {uses.length ? (
            uses.map((i) => (
              <a
                className="mr-3 text-accent"
                key={`${i.kind}/${i.id}`}
                href={
                  i.kind === "modules"
                    ? "#/devices/modules"
                    : i.kind === "lib"
                      ? "#/scripts"
                      : `#/${i.kind}/${i.id}`
                }
              >
                {i.name}
              </a>
            ))
          ) : (
            <p>Не используется в сохранённом коде.</p>
          )}
          <button onClick={() => setUses(null)}>Закрыть</button>
        </div>
      )}
      {board?.hal === "bench" && <AdafruitBench onError={onError} />}
      <div className="divide-y divide-line">
        {catalog?.drivers
          .filter((d) =>
            (d.package + " " + (catalog.profiles[d.chip]?.title || ""))
              .toLowerCase()
              .includes(query.toLowerCase()),
          )
          .map((d) => (
            <div
              key={d.package}
              className="flex flex-wrap items-center gap-3 py-3"
            >
              <div className="min-w-0 flex-1">
                <a
                  href={d.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent"
                >
                  {d.package}
                </a>
                <p className="text-xs text-muted">
                  {d.version || "не установлен"} · {d.compatibility}
                  {catalog.profiles[d.chip] &&
                    ` · I²C ${"0x" + catalog.profiles[d.chip].address.toString(16)}`}
                </p>
              </div>
              <button
                disabled={busy || job?.state === "running"}
                onClick={() => act(() => installDriver(d.package, setJob))}
              >
                {d.version ? "Обновить" : "Установить"}
              </button>
              <button
                disabled={busy || job?.state === "running"}
                onClick={() => {
                  const version = prompt(
                    `Версия ${d.package} (из PyPI)`,
                    d.version || "",
                  );
                  if (version?.trim())
                    act(() => installDriver(d.package, setJob, version.trim()));
                }}
              >
                Версия…
              </button>
              {d.version && <button disabled={busy || job?.state === "running"} onClick={() => act(async () => {
                const result = await request(`/adafruit/example/${d.package}`, {});
                navigate({page: "scripts", id: result.id});
              })}>Пример из библиотеки</button>}
              <button
                onClick={() =>
                  request(`/adafruit/uses/${d.package}`).then(
                    setUses,
                    (e: Error) => onError(e.message),
                  )
                }
              >
                Где используется
              </button>
              {d.requested && (
                <button
                  disabled={busy || job?.state === "running"}
                  className="text-err"
                  onClick={() =>
                    act(async () => {
                      const u = await request(`/adafruit/uses/${d.package}`);
                      if (
                        !confirm(
                          `Удалить ${d.package}? ${u.length ? "Используется: " + u.map((i: { name: string }) => i.name).join(", ") : ""}`,
                        )
                      )
                        return;
                      const r = await request("/adafruit/change", {
                        package: d.package,
                        remove: true,
                        confirmed: true,
                      });
                      setJob(r.job);
                    })
                  }
                >
                  Удалить
                </button>
              )}
            </div>
          ))}
      </div>
      {wizard && (
        <DeviceWizard
          board={board}
          onClose={() => setWizard(false)}
          onError={onError}
        />
      )}
    </section>
  );
}

export function DeviceWizard({
  board,
  kind,
  id,
  currentBlockCode,
  onInsert,
  onClose,
  onError,
}: {
  board: Board | null;
  kind?: string;
  id?: string;
  currentBlockCode?: string;
  onInsert?: (code: string) => void;
  onClose: () => void;
  onError: (m: string) => void;
}) {
  const [catalog, setCatalog] = useState<Catalog>();
  const [chip, setChip] = useState("bme280");
  const [iface, setIface] = useState("i2c1");
  const [address, setAddress] = useState("0x76");
  const [columns, setColumns] = useState(20);
  const [rows, setRows] = useState(4);
  const [block, setBlock] = useState(kind === "blocks");
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [preview, setPreview] = useState("");
  const [moduleId, setModuleId] = useState("adafruit_bme280");
  const [moduleVersion, setModuleVersion] = useState(1);
  useEffect(() => {
    request("/adafruit").then(setCatalog, (e: Error) => onError(e.message));
  }, []);
  const selectChip = (value: string) => {
    setChip(value);
    setModuleId(`adafruit_${value}`);
    setAddress("0x" + (catalog?.profiles[value].address ?? 0x76).toString(16));
    setPreview("");
    setBlock(kind === "blocks");
  };
  const prepare = async () => {
    const fresh: Catalog = await request("/adafruit");
    const names =
      chip === "lcd"
        ? ["pcf8574", "charlcd"]
        : chip === "ssd1306"
          ? [chip, "framebuf"]
          : [chip];
    for (const name of names) {
      const pkg = "adafruit-circuitpython-" + name;
      if (!fresh.drivers.some((d) => d.package === pkg && d.version)) {
        if (!(await installDriver(pkg, setJob))) return false;
      }
    }
    setCatalog(await request("/adafruit"));
    return true;
  };
  const connect = async () => {
    setBusy(true);
    try {
      if (!(await prepare())) return;
      const body = {
        chip,
        interface: iface,
        address: Number(address),
        columns,
        rows,
        kind,
        id,
        block,
      };
      const result = await request("/adafruit/connect", body);
      if (block) {
        navigate({ page: "blocks", id: result.block.id });
        onClose();
      } else setPreview(result.code);
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !busy) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label="Подключить устройство"
        className="max-h-[90vh] w-[min(680px,95vw)] overflow-auto rounded border border-line bg-panel p-5"
      >
        <div className="flex justify-between">
          <h2 className="font-semibold">Подключить устройство</h2>
          <button disabled={busy} aria-label="Закрыть" onClick={onClose}>
            ×
          </button>
        </div>
        <p className="my-3 text-xs text-warn">
          Логика Napi-C — 3,3 В. Модули с подтяжками SDA/SCL к 5 В требуют
          преобразователя уровней. Установка доверенных драйверов выполняется из
          PyPI.
        </p>
        <fieldset disabled={busy}>
          <label className="block my-3">
            Устройство
            <select
              className="ml-2 bg-bg p-2"
              value={chip}
              onChange={(e) => selectChip(e.target.value)}
            >
              {Object.entries(catalog?.profiles || {})
                .filter(
                  ([key]) =>
                    kind !== "blocks" ||
                    ["bme280", "sht4x", "pca9685"].includes(key),
                )
                .map(([key, p]) => (
                  <option key={key} value={key}>
                    {p.title}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Интерфейс
            <select
              className="mx-2 bg-bg p-2"
              value={iface}
              onChange={(e) => {
                setIface(e.target.value);
                setPreview("");
              }}
            >
              {board?.interfaces
                .filter((i) => i.kind === "i2c" && i.enabled && !i.locked)
                .map((i) => (
                  <option value={i.id} key={i.id}>
                    {i.title} ·{" "}
                    {Object.entries(i.pins)
                      .map(([p, r]) => `${p} ${r}`)
                      .join(", ")}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Адрес
            <input
              className="mx-2 w-24 border border-line bg-bg p-2"
              value={address}
              onChange={(e) => {
                setAddress(e.target.value);
                setPreview("");
              }}
            />
          </label>
          <p className="my-2 text-xs text-muted">
            Выключенные интерфейсы:{" "}
            <a className="text-accent" href="#/board">
              включить через план на странице платы
            </a>
            . Занятость адреса проверяется перед вставкой.
          </p>
          {chip === "lcd" && (
            <div className="my-3">
              <label>
                Колонки
                <select
                  className="mx-2 bg-bg"
                  value={columns}
                  onChange={(e) => {
                    setColumns(Number(e.target.value));
                    setPreview("");
                  }}
                >
                  {[16, 20].map((n) => (
                    <option key={n}>{n}</option>
                  ))}
                </select>
              </label>
              <label>
                Строки
                <select
                  className="mx-2 bg-bg"
                  value={rows}
                  onChange={(e) => {
                    setRows(Number(e.target.value));
                    setPreview("");
                  }}
                >
                  {[2, 4].map((n) => (
                    <option key={n}>{n}</option>
                  ))}
                </select>
              </label>
            </div>
          )}
          {["bme280", "sht4x", "pca9685"].includes(chip) && (
            <label className="my-3 block">
              <input
                type="checkbox"
                disabled={kind === "blocks"}
                checked={block}
                onChange={(e) => {
                  setBlock(e.target.checked);
                  setPreview("");
                }}
              />{" "}
              Создать новый блок из драйвера
            </label>
          )}
        </fieldset>
        {block && (
          <div className="my-3 rounded border border-line p-3">
            <p>Сохранить драйвер, блок и модель стенда как ZIP-модуль</p>
            <input
              aria-label="ID модуля"
              disabled={busy}
              className="my-2 bg-bg p-2"
              value={moduleId}
              onChange={(e) => setModuleId(e.target.value)}
            />
            <input
              aria-label="Версия модуля"
              type="number"
              min={1}
              max={9999}
              disabled={busy}
              className="mx-2 w-20 bg-bg p-2"
              value={moduleVersion}
              onChange={(e) => setModuleVersion(Number(e.target.value))}
            />
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  if (!(await prepare())) return;
                  const r = await fetch("/pioneer/api/adafruit/module", {
                    method: "POST",
                    headers: {
                      "X-Pioneer": "1",
                      "Content-Type": "application/json",
                    },
                    body: JSON.stringify({
                      chip,
                      interface: iface,
                      address: Number(address),
                      module_id: moduleId,
                      version: moduleVersion,
                      code: currentBlockCode,
                    }),
                  });
                  if (!r.ok) throw new Error((await r.json()).detail);
                  const url = URL.createObjectURL(await r.blob());
                  const a = document.createElement("a");
                  a.href = url;
                  a.download = `${moduleId}-v${moduleVersion}.zip`;
                  a.click();
                  setTimeout(() => URL.revokeObjectURL(url), 1000);
                } catch (e) {
                  onError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {currentBlockCode
                ? "Скачать модуль с текущим кодом блока"
                : "Скачать модуль"}
            </button>
          </div>
        )}
        {job && (
          <pre
            role="status"
            className="max-h-36 overflow-auto whitespace-pre-wrap text-xs"
          >
            {job.state}
            {"\n"}
            {job.log}
          </pre>
        )}
        <button
          className="my-3 rounded bg-accent px-4 py-2 text-white disabled:opacity-40"
          disabled={
            busy ||
            !board?.interfaces.some((i) => i.id === iface && i.enabled) ||
            !Number.isInteger(Number(address))
          }
          onClick={connect}
        >
          {busy ? "Подготавливаю…" : block ? "Создать блок" : "Подготовить код"}
        </button>
        {preview && (
          <>
            <pre className="max-h-64 overflow-auto rounded bg-bg p-3 text-xs">
              {preview}
            </pre>
            <button
              disabled={busy}
              className="mt-3 text-accent"
              onClick={async () => {
                setBusy(true);
                try {
                  if (onInsert) {
                    await request("/adafruit/connect", {
                      chip,
                      interface: iface,
                      address: Number(address),
                      columns,
                      rows,
                      kind,
                      id,
                      bind: true,
                    });
                    onInsert(preview);
                  } else {
                    const s = await api.scriptCreate(
                      `Adafruit ${chip}`,
                      preview,
                    );
                    await request("/adafruit/connect", {
                      chip,
                      interface: iface,
                      address: Number(address),
                      columns,
                      rows,
                      kind: "scripts",
                      id: s.id,
                      bind: true,
                    });
                    navigate({ page: "scripts", id: s.id });
                  }
                  onClose();
                } catch (e) {
                  onError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {onInsert ? "Вставить в текущий файл" : "Создать скрипт-пример"}
            </button>
          </>
        )}
      </section>
    </div>
  );
}
