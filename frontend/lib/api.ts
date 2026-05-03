const PROD_API = "https://amta-backend-live-production.up.railway.app";
const PROD_WS = "wss://amta-backend-live-production.up.railway.app/api/ws";

export const API = process.env.NEXT_PUBLIC_API_URL || PROD_API;
export const WS = process.env.NEXT_PUBLIC_WS_URL || PROD_WS;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const attempts = 3;
  let lastErr: Error | null = null;
  for (let i = 0; i < attempts; i++) {
    try {
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
        // Retry only transient upstream failures.
        if (res.status >= 500 && i < attempts - 1) {
          await sleep(400 * (i + 1));
          continue;
        }
        throw new Error(`${res.status} ${detail}`);
      }
      return res.json();
    } catch (e: any) {
      lastErr = e instanceof Error ? e : new Error(String(e));
      if (i < attempts - 1) {
        await sleep(400 * (i + 1));
        continue;
      }
      break;
    }
  }
  throw lastErr || new Error("request failed");
}
