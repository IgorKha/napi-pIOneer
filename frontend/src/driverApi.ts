export async function driverRequest(path: string, body?: unknown, raw?: File) {
  const r = await fetch(`/pioneer/api${path}`, {method: raw || body !== undefined ? "POST" : "GET",
    headers: {"X-Pioneer": "1", ...(raw ? {} : {"Content-Type": "application/json"})}, body: raw ?? (body !== undefined ? JSON.stringify(body) : undefined)});
  const data = await r.json();
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${r.status}`);
  return data;
}
