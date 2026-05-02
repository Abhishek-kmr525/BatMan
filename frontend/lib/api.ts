const PROD_API = "https://amta-backend-live-production.up.railway.app";
const PROD_WS = "wss://amta-backend-live-production.up.railway.app/api/ws";

export const API = process.env.NEXT_PUBLIC_API_URL || PROD_API;
export const WS = process.env.NEXT_PUBLIC_WS_URL || PROD_WS;

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API}/api${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const raw = await res.text();
    let detail = raw;
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed?.detail === "string") detail = parsed.detail;
      else if (typeof parsed?.detail?.message === "string") detail = parsed.detail.message;
      else if (typeof parsed?.message === "string") detail = parsed.message;
      else detail = raw;
    } catch {
      detail = raw;
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}
