/* Copyright 2026 会议库 contributors
 * SPDX-License-Identifier: Apache-2.0 */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// kind 区分回答的两段：ground=依据录音材料的回答，extra=AI 补充（非录音内容）
export async function readSse(
  path: string,
  body: unknown,
  onToken: (t: string, kind?: string) => void,
): Promise<unknown> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!res.ok || !res.body) throw new Error(await res.text());
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let doneMsg: unknown = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() || "";
    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const ev = JSON.parse(line.slice(5).trim());
      if (ev.event === "token") onToken(ev.text || "", ev.kind || "ground");
      if (ev.event === "done") doneMsg = ev.message;
    }
  }
  return doneMsg;
}
