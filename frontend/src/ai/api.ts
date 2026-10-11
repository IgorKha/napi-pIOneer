import { ApiError, type Graph, type Op, type Validation } from "../api";

export interface AISettings {
  provider: "anthropic" | "openai" | "custom"; model: string; key_secret: string;
  base_url: string; effort: "default" | "none" | "low" | "medium" | "high" | "xhigh";
  max_tokens: number; search: "off" | "builtin" | "searxng"; search_url: string;
}
export interface AIArtifact {
  id: string; kind: "proposal" | "draft" | "file"; status: string; summary: string;
  ops?: Op[]; graph?: Graph; rev?: number; validation?: Validation;
  name?: string; code?: string; path?: string; original?: string; base_hash?: string | null;
}
export interface AIAttachment {name: string; type: string; content: string}
export interface AIMessage {
  id: string; role: "user" | "assistant"; text: string; error?: string;
  attachments?: {name: string; type: string}[]; model?: string;
  artifacts?: AIArtifact[]; citations?: { url: string; title: string }[];
  usage?: Record<string, number>;
}
export type AIEvent = { type: string; id?: string; text?: string; error?: string; name?: string;
  artifact?: AIArtifact; message?: AIMessage; usage?: Record<string, number>; url?: string; title?: string };

async function request(path: string, init: RequestInit = {}) {
  const response = await fetch("/pioneer/api/ai" + path, { ...init,
    headers: { "X-Pioneer": "1", "Content-Type": "application/json", ...init.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(typeof body.detail === "string" ? body.detail : `AI: HTTP ${response.status}`, response.status);
  }
  return response;
}

export const ai = {
  settings: async (): Promise<{ settings: AISettings; configured: boolean; busy: boolean }> => (await request("/settings")).json(),
  save: async (settings: AISettings, api_key?: string) => (await request("/settings", {
    method: "PUT", body: JSON.stringify({ ...settings, ...(api_key ? { api_key } : {}) }),
  })).json(),
  history: async (id: string): Promise<{ messages: AIMessage[] }> => (await request(`/chats/${id}`)).json(),
  reset: async (id: string) => request(`/chats/${id}`, { method: "DELETE" }),
  decision: async (id: string, artifact: string, status: string) => request(`/chats/${id}/artifacts/${artifact}`, {
    method: "PUT", body: JSON.stringify({ status }),
  }),
  acceptFile: async (id: string, artifact: string, code: string): Promise<{path: string}> => {
    const response = await request(`/chats/${id}/artifacts/${artifact}/file`, {method: "PUT", body: JSON.stringify({code})});
    if (id.startsWith("block:")) window.dispatchEvent(new Event("pioneer-reference-changed"));
    return response.json();
  },
  stream: async (id: string, text: string, signal: AbortSignal, receive: (event: AIEvent) => void, model?: string, attachments?: AIAttachment[]) => {
    const response = await request(`/chats/${id}/messages`, { method: "POST", signal, body: JSON.stringify({ text, model, attachments }) });
    if (!response.body) throw new Error("Поток AI недоступен");
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let index: number;
        while ((index = buffer.indexOf("\n\n")) >= 0) {
          const record = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          for (const line of record.split("\n")) if (line.startsWith("data: ")) receive(JSON.parse(line.slice(6)));
        }
        if (done) break;
      }
    } finally { reader.releaseLock(); }
  },
};
