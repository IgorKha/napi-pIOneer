import { useEffect, useState } from "react";

/** Маршруты на хэше (#/board, #/scripts, #/scripts/<id>) — работают за любым префиксом nginx. */
export type Route = { page: "board" } | { page: "scripts"; id?: string };

function parse(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "scripts") return { page: "scripts", id: parts[1] };
  return { page: "board" };
}

export function href(r: Route): string {
  return r.page === "scripts" ? `#/scripts${r.id ? `/${r.id}` : ""}` : "#/board";
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

/** owner id из ResourceManager ("script:<id>") -> куда провалиться по клику. */
export function ownerRoute(owner: string): Route | null {
  return owner.startsWith("script:") ? { page: "scripts", id: owner.slice(7) } : null;
}
