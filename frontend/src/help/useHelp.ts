import { useEffect, useState } from "react";
import { api, type HelpReference } from "../api";

// Справочник генерируется бэкендом из кода (GET /help) — грузим один раз на вкладку.
let cache: Promise<HelpReference> | null = null;

export function loadHelp(): Promise<HelpReference> {
  return (cache ??= api.help().catch((e) => {
    cache = null;
    throw e;
  }));
}

export function useHelp(): HelpReference | null {
  const [ref, setRef] = useState<HelpReference | null>(null);
  useEffect(() => {
    loadHelp().then(setRef, () => {});
  }, []);
  return ref;
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
