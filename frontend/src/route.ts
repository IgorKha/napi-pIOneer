import { useEffect, useState } from "react";

/** Маршруты на хэше (#/board, #/scripts/<id>, #/projects/<id>, #/help/<раздел>/<якорь>) —
 * работают за любым префиксом nginx. */
export type Route =
  | { page: "board" }
  | { page: "scripts"; id?: string }
  | { page: "projects"; id?: string; group?: string; inst?: string } // inst — путь ноды-группы: "g" или "g2/g1"
  | { page: "blocks"; id?: string }
  | { page: "secrets" }
  | { page: "help"; section?: string; anchor?: string };

function parse(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "scripts") return { page: "scripts", id: parts[1] };
  if (parts[0] === "projects")
    return { page: "projects", id: parts[1], ...(parts[2] === "group" ? { group: parts[3], inst: parts[4] } : {}) };
  if (parts[0] === "blocks") return { page: "blocks", id: parts[1] };
  if (parts[0] === "secrets") return { page: "secrets" };
  if (parts[0] === "help") return { page: "help", section: parts[1], anchor: parts[2] };
  return { page: "board" };
}

export function href(r: Route): string {
  switch (r.page) {
    case "projects":
      return `#/projects${r.id ? `/${r.id}` : ""}${r.group ? `/group/${r.group}/${encodeURIComponent(r.inst ?? "")}` : ""}`;
    case "scripts":
    case "blocks":
      return `#/${r.page}${r.id ? `/${r.id}` : ""}`;
    case "help":
      return `#/help${r.section ? `/${r.section}` : ""}${r.anchor ? `/${encodeURIComponent(r.anchor)}` : ""}`;
    case "secrets":
      return "#/secrets";
    default:
      return "#/board";
  }
}

export function navigate(r: Route) {
  location.hash = href(r);
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(location.hash));
  useEffect(() => {
    const on = () => setRoute(parse(location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

/** owner id из ResourceManager ("script:<id>", "project:<id>") -> куда провалиться по клику. */
export function ownerRoute(owner: string): Route | null {
  if (owner.startsWith("script:")) return { page: "scripts", id: owner.slice(7) };
  if (owner.startsWith("project:")) return { page: "projects", id: owner.slice(8) };
  return null;
}
