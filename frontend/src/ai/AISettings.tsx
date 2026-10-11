import { useEffect, useState } from "react";
import { ai, type AISettings as Settings } from "./api";

export function AISettings({ onError }: { onError: (m: string) => void }) {
  const [form, setForm] = useState<Settings>();
  const [key, setKey] = useState("");
  const [configured, setConfigured] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { ai.settings().then((r) => { setForm(r.settings); setConfigured(r.configured); }, (e: Error) => onError(e.message)); }, [onError]);
  if (!form) return null;
  const field = "rounded border border-line bg-bg px-2 py-1.5 outline-none focus:border-accent min-w-0";
  const set = <K extends keyof Settings>(name: K, v: Settings[K]) => setForm({ ...form, [name]: v });
  return <section>
    <h2 className="mb-2 text-lg font-semibold">AI-помощник</h2>
    <p className="mb-4 text-muted">Чат в редакторах проектов и скриптов. Предложенные изменения принимаете вы. Ключ хранится на бэкенде.
      {configured ? " Ключ задан." : " Добавьте ключ провайдера."}</p>
    <form onSubmit={async (e) => {
      e.preventDefault(); setBusy(true);
      try { const r = await ai.save(form, key); setForm(r.settings); setConfigured(r.configured); setKey(""); }
      catch (err) { onError((err as Error).message); } finally { setBusy(false); }
    }}>
      <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2">
        <label htmlFor="ai-provider">Провайдер</label>
        <select id="ai-provider" value={form.provider} className={field} onChange={(e) => {
          const p = e.target.value as Settings["provider"];
          setForm({ ...form, provider: p, model: p === "anthropic" ? "claude-opus-5-5" : p === "openai" ? "gpt-5.4-mini" : "",
                    search: p === "custom" ? "off" : "builtin", effort: "default" });
        }}><option value="anthropic">Anthropic</option><option value="openai">OpenAI</option><option value="custom">OpenAI-совместимый</option></select>
        <label htmlFor="ai-model">Модель</label><input id="ai-model" className={field} value={form.model} required onChange={(e) => set("model", e.target.value)} />
        {form.provider === "custom" && <><label htmlFor="ai-base">Base URL</label><input id="ai-base" className={field} value={form.base_url} required placeholder="https://server/v1" onChange={(e) => set("base_url", e.target.value)} /></>}
        <label htmlFor="ai-key">Новый ключ</label><input id="ai-key" type="password" autoComplete="new-password" className={field} value={key} placeholder={configured ? "Задан · оставить прежний" : "API key"} onChange={(e) => setKey(e.target.value)} />
        <label htmlFor="ai-secret">Имя секрета</label><input id="ai-secret" className={field} value={form.key_secret} onChange={(e) => set("key_secret", e.target.value)} />
        <label htmlFor="ai-effort">Effort</label><select id="ai-effort" className={field} value={form.effort} onChange={(e) => set("effort", e.target.value as Settings["effort"])}>
          {(form.provider === "anthropic" ? ["default", "low", "medium", "high"] : ["default", "none", "low", "medium", "high", "xhigh"]).map((v) => <option key={v}>{v}</option>)}
        </select>
        <label htmlFor="ai-limit">Токенов на ответ</label><input id="ai-limit" type="number" min={512} max={16384} value={form.max_tokens} className={field} onChange={(e) => set("max_tokens", Number(e.target.value))} />
        <label htmlFor="ai-search">Веб-поиск</label><select id="ai-search" className={field} value={form.search} onChange={(e) => set("search", e.target.value as Settings["search"])}>
          <option value="off">Выключен</option>{form.provider !== "custom" && <option value="builtin">Встроенный у провайдера</option>}<option value="searxng">SearXNG</option>
        </select>
        {form.search === "searxng" && <><label htmlFor="ai-search-url">URL поиска</label><input id="ai-search-url" className={field} value={form.search_url} required onChange={(e) => set("search_url", e.target.value)} /></>}
      </div>
      <button disabled={busy} className="mt-4 rounded bg-accent px-3 py-1.5 text-white disabled:opacity-40">Сохранить AI</button>
    </form>
  </section>;
}
