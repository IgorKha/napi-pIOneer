import { useEffect, useState } from "react";
import { api, type HelpReference } from "../api";

// Справочник генерируется бэкендом из кода (GET /help) — грузим один раз на вкладку.
let cache: Promise<HelpReference> | null = null;
// Invalidate even while no help/editor consumer is mounted.
window.addEventListener("pioneer-reference-changed", () => { cache = null; });

export function loadHelp(): Promise<HelpReference> {
  return (cache ??= api.help().catch((e) => {
    cache = null;
    throw e;
  }));
}

/** Explicit refresh and schema mutations update all mounted consumers, including Monaco. */
export function useHelpResource() {
  const [reference, setReference] = useState<HelpReference | null>(null);
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const refresh = () => { setGeneration(g => g + 1); };
    window.addEventListener("pioneer-reference-changed", refresh);
    return () => window.removeEventListener("pioneer-reference-changed", refresh);
  }, []);
  useEffect(() => {
    let active = true;
    setError("");
    loadHelp().then(r => { if (active) setReference(r); }, (e: Error) => { if (active) setError(`Не удалось загрузить справку: ${e.message}`); });
    return () => { active = false; };
  }, [generation]);
  return { reference, error, reload: () => window.dispatchEvent(new Event("pioneer-reference-changed")) };
}

export function useHelp(): HelpReference | null {
  return useHelpResource().reference;
}

/** Якорь, совпадающий с Python help.slug(): кириллица — тоже «слово». */
export function slug(text: string): string {
  return text.toLowerCase().replace(/[^\p{L}\p{N}_]+/gu, "-").replace(/^-+|-+$/g, "");
}

/** Статья каталога ошибок для строки лога (по `<!-- match: … -->` из docs/sdk/errors.md). */
export function errorArticle(ref: HelpReference | null, text: string): string | null {
  if (!ref) return null;
  for (const e of ref.errors) {
    try {
      if (new RegExp(e.pattern).test(text)) return e.anchor;
    } catch {
      /* кривой шаблон в errors.md не должен ломать лог */
    }
  }
  return null;
}
