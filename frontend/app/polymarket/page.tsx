"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { LineChart, Line, XAxis, YAxis, ResponsiveContainer, Tooltip, CartesianGrid } from "recharts";

type PolyBotStatus = {
  status: string;
  state: string;
  uptime_seconds: number;
  trades_today: number;
  scanned_markets_today: number;
  last_scan_count: number;
  last_candidate_count: number;
  active_positions: number;
  max_concurrent_positions: number;
  mode_guard?: {
    mode: string;
    live_enabled: boolean;
  };
};

type PolyWallet = {
  balance: number;
  total_pnl: number;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  live_error?: string | null;
};

type PolySummary = {
  total_count: number;
  open_count: number;
  closed_count: number;
  today_opened_count: number;
  today_closed_count: number;
};

type PolyTrade = {
  id: string;
  market_id: string;
  market_title: string;
  direction: "YES" | "NO";
  amount: number;
  entry_price: number;
  exit_price?: number | null;
  current_price?: number | null;
  pnl: number;
  status: string;
  agent_score: number;
  opened_at: string;
  closed_at?: string | null;
};

type PolyMarket = {
  id: string;
  title: string;
  yes_price: number;
  no_price: number;
  volume: number;
  time_to_close_seconds: number;
  slug: string;
  event_slug: string;
};

type Candle = {
  t: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

type RangeLabel = "Today" | "This Week" | "This Month" | "This Year" | "All Time";

function inDays(d: Date, n: number) {
  return Date.now() - d.getTime() <= n * 24 * 60 * 60 * 1000;
}

function fmtCurrency(n: number) {
  return `${n >= 0 ? "+" : "-"}$${Math.abs(n).toFixed(2)}`;
}

function isSameLocalDay(a: Date, b: Date) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function statForRange(label: RangeLabel, closed: PolyTrade[]) {
  const today = new Date();
  const filtered = closed.filter((t) => {
    const dt = new Date(t.closed_at || t.opened_at);
    if (label === "Today") return isSameLocalDay(dt, today);
    if (label === "This Week") return inDays(dt, 7);
    if (label === "This Month") return inDays(dt, 30);
    if (label === "This Year") return inDays(dt, 365);
    return true;
  });
  const pnl = filtered.reduce((a, t) => a + (t.pnl || 0), 0);
  const wins = filtered.filter((t) => (t.pnl || 0) > 0).length;
  const losses = filtered.filter((t) => (t.pnl || 0) < 0).length;
  const total = filtered.length;
  const strike = total ? (wins / total) * 100 : 0;
  return { label, pnl, wins, losses, total, strike };
}

export default function PolymarketPage() {
  const [bot, setBot] = useState<PolyBotStatus | null>(null);
  const [wallet, setWallet] = useState<PolyWallet | null>(null);
  const [summary, setSummary] = useState<PolySummary | null>(null);
  const [openTrades, setOpenTrades] = useState<PolyTrade[]>([]);
  const [closedTrades, setClosedTrades] = useState<PolyTrade[]>([]);
  const [allTrades, setAllTrades] = useState<PolyTrade[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [markets, setMarkets] = useState<PolyMarket[]>([]);
  const [interval, setIntervalLabel] = useState<"5m" | "15m">("5m");
  const [candles, setCandles] = useState<Candle[]>([]);

  async function refresh() {
    try {
      const [b, w, s, o, c, a] = await Promise.all([
        api<PolyBotStatus>("/polymarket/bot/status"),
        api<PolyWallet>("/polymarket/wallet"),
        api<PolySummary>("/polymarket/trades/summary"),
        api<PolyTrade[]>("/polymarket/trades?status=open&limit=50&page=1"),
        api<PolyTrade[]>("/polymarket/trades?status=closed&limit=100&page=1"),
        api<PolyTrade[]>("/polymarket/trades?status=all&limit=500&page=1"),
      ]);
      setBot(b);
      setWallet(w);
      setSummary(s);
      setOpenTrades(o);
      setClosedTrades(c);
      setAllTrades(a);
      const mm = await api<PolyMarket[]>("/polymarket/markets?limit=120");
      const filtered = mm.filter((m) => {
        const hay = `${m.title} ${m.slug} ${m.event_slug}`.toLowerCase();
        const isUpDown = hay.includes("up or down") || hay.includes("updown");
        const isWantedWindow = hay.includes("5m") || hay.includes("15m") || hay.includes("5 min") || hay.includes("15 min");
        return isUpDown && isWantedWindow;
      });
      setMarkets(filtered.slice(0, 30));
      setError(null);
    } catch (e: any) {
      setError(e?.message || "refresh failed");
    }
  }

  async function startBot() {
    setBusy(true);
    try {
      await api("/polymarket/bot/start", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function stopBot() {
    setBusy(true);
    try {
      await api("/polymarket/bot/stop", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function resetPaper() {
    const pass = prompt("Passcode", "9472");
    if (!pass) return;
    setBusy(true);
    try {
      await api("/maintenance/poly/reset", {
        method: "POST",
        body: JSON.stringify({ passcode: pass, balance: 20 }),
      });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function toggleMode() {
    if (!bot?.mode_guard) return;
    const current = bot.mode_guard.mode;
    setBusy(true);
    try {
      if (current === "paper") {
        await api("/mode/request-live", {
          method: "POST",
          body: JSON.stringify({ platform: "polymarket" }),
        });
        const pass = prompt("Enter Live Mode Passcode to Arm (9472):");
        if (!pass) {
          await api("/mode/set-paper", {
            method: "POST",
            body: JSON.stringify({ platform: "polymarket" }),
          });
        } else {
          await api("/mode/confirm-live", {
            method: "POST",
            body: JSON.stringify({ platform: "polymarket", passcode: pass }),
          });
        }
      } else {
        await api("/mode/set-paper", {
          method: "POST",
          body: JSON.stringify({ platform: "polymarket" }),
        });
      }
      await refresh();
    } catch (e: any) {
      alert("Mode toggle failed: " + e.message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const loadCandles = async () => {
      try {
        const res = await api<{ symbol: string; interval: "5m" | "15m"; candles: Candle[] }>(
          `/polymarket/candles?interval=${interval}&limit=80&symbol=BTCUSDT`
        );
        setCandles(res.candles || []);
      } catch {
        setCandles([]);
      }
    };
    void loadCandles();
    const id = setInterval(() => void loadCandles(), 15000);
    return () => clearInterval(id);
  }, [interval]);

  const ranges: RangeLabel[] = ["Today", "This Week", "This Month", "This Year", "All Time"];
  const stats = ranges.map((r) => statForRange(r, closedTrades));
  const last30 = statForRange("This Month", closedTrades);
  const candidateCount = bot?.last_candidate_count ?? 0;
  const winRatePct = wallet?.win_rate ?? 0;

  const balanceSeries = (() => {
    if (!wallet) return [] as { t: string; balance: number }[];
    const pts: { ts: number; label: string; delta: number }[] = [];
    for (const t of allTrades) {
      const opened = new Date(t.opened_at).getTime();
      if (!Number.isNaN(opened)) {
        pts.push({
          ts: opened,
          label: new Date(opened).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
          delta: -Math.abs(t.amount || 0),
        });
      }
      if (t.closed_at) {
        const closedTs = new Date(t.closed_at).getTime();
        if (!Number.isNaN(closedTs)) {
          pts.push({
            ts: closedTs,
            label: new Date(closedTs).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
            delta: (t.amount || 0) + (t.pnl || 0),
          });
        }
      }
    }
    if (pts.length === 0) {
      return [{ t: "Now", balance: Number(wallet.balance.toFixed(2)) }];
    }
    pts.sort((a, b) => a.ts - b.ts);
    const totalDelta = pts.reduce((acc, p) => acc + p.delta, 0);
    let bal = wallet.balance - totalDelta;
    const out = [{ t: "Start", balance: Number(bal.toFixed(2)) }];
    for (const p of pts) {
      bal += p.delta;
      out.push({ t: p.label, balance: Number(bal.toFixed(2)) });
    }
    return out.slice(-250);
  })();

  const running = bot?.status === "running";
  const pnl = wallet?.total_pnl ?? 0;
  const openedToday = summary?.today_opened_count ?? bot?.trades_today ?? 0;
  const scannedToday = bot?.scanned_markets_today ?? 0;

  return (
    <div className="container dashx">
      <div className="dashx-top">
        <div>
          <h1>🤖 AMTA Dashboard</h1>
          <div className="sub">Polymarket paper trading analytics</div>
        </div>
        <div className="dashx-actions">
          {bot?.mode_guard && (
            <button
              className={`btn ${bot.mode_guard.mode === "live_armed" ? "btn-stop" : "btn-secondary"}`}
              onClick={toggleMode}
              disabled={busy || !bot.mode_guard.live_enabled}
              title={!bot.mode_guard.live_enabled ? "Live mode disabled in config" : ""}
            >
              {bot.mode_guard.mode === "live_armed" ? "🔴 Live Armed" : "🟢 Paper Mode"}
            </button>
          )}
          {!running ? (
            <button className="btn btn-start" onClick={startBot} disabled={busy}>▶ Start Bot</button>
          ) : (
            <button className="btn btn-stop" onClick={stopBot} disabled={busy}>■ Stop Bot</button>
          )}
          <button className="btn btn-secondary" onClick={refresh} disabled={busy}>Refresh</button>
          <button className="btn btn-secondary" onClick={resetPaper} disabled={busy}>Reset $20</button>
        </div>
      </div>

      {error && (
        <div className="sub" style={{ marginTop: 8, color: "#ff7b7b", marginBottom: 8 }}>
          Live refresh issue: {error}
        </div>
      )}

      <div className="dashx-hero-row">
        <div className="card dashx-hero">
          <div className="dashx-hero-head">
            <div className="dashx-hero-icon hero-green" aria-hidden>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12a7 7 0 0 1 14 0"/><path d="M8.5 12a3.5 3.5 0 0 1 7 0"/><circle cx="12" cy="12" r="1"/></svg>
            </div>
            <span className={`dashx-hero-badge ${running ? "ok" : "muted"}`}>
              <span className="dot"/>{running ? "Live" : "Idle"}
            </span>
          </div>
          <div className="dashx-hero-label">Active Signals</div>
          <div className="dashx-hero-value">
            {bot?.active_positions ?? 0}
            <span className="dashx-hero-delta pos">+{summary?.today_opened_count ?? 0} today</span>
          </div>
        </div>

        <div className="card dashx-hero">
          <div className="dashx-hero-head">
            <div className="dashx-hero-icon hero-blue" aria-hidden>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/></svg>
            </div>
            <span className="dashx-hero-trend pos">↗ {wallet ? wallet.wins : 0}W</span>
          </div>
          <div className="dashx-hero-label">Win Rate</div>
          <div className="dashx-hero-value">{winRatePct.toFixed(1)}%</div>
          <div className="dashx-hero-bar">
            <div className="dashx-hero-bar-fill" style={{ width: `${Math.min(100, Math.max(0, winRatePct))}%` }} />
          </div>
        </div>

        <div className="card dashx-hero">
          <div className="dashx-hero-head">
            <div className="dashx-hero-icon hero-violet" aria-hidden>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 17 9 11 13 15 21 7"/><polyline points="14 7 21 7 21 14"/></svg>
            </div>
            <span className={`dashx-hero-trend ${last30.pnl >= 0 ? "pos" : "neg"}`}>
              ↗ {last30.total} trades
            </span>
          </div>
          <div className="dashx-hero-label">Total Profit (30d)</div>
          <div className={`dashx-hero-value gradient ${last30.pnl < 0 ? "neg-gradient" : ""}`}>
            {last30.pnl >= 0 ? "+" : "-"}${Math.abs(last30.pnl).toFixed(2)}
          </div>
        </div>

        <div className="card dashx-hero">
          <div className="dashx-hero-head">
            <div className="dashx-hero-icon hero-amber" aria-hidden>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
            </div>
            <span className="dashx-hero-badge pending"><span className="dot"/>Pending</span>
          </div>
          <div className="dashx-hero-label">Pending Signals</div>
          <div className="dashx-hero-value">
            {candidateCount}
            <span className="dashx-hero-delta muted">awaiting execution</span>
          </div>
        </div>
      </div>

      <div className="dashx-grid-top">
        {stats.map((s) => (
          <div className="card dashx-kpi" key={s.label}>
            <div className="dashx-kpi-title">{s.label}</div>
            <div className={`dashx-kpi-main ${s.pnl >= 0 ? "pos" : "neg"}`}>{fmtCurrency(s.pnl)}</div>
            <div className="dashx-kpi-sub">{Math.abs(s.pnl).toFixed(2)} R:R</div>
            <div className="dashx-kpi-sub">{s.strike.toFixed(2)}% strike rate</div>
            <div className="dashx-kpi-badges">
              <span className="dashx-pill">{s.total}</span>
              <span className="dashx-pill pos">{s.wins}</span>
              <span className="dashx-pill neg">{s.losses}</span>
            </div>
          </div>
        ))}

        <div className="card dashx-account">
          <div className="dashx-account-row">
            <span>Account Balance</span>
            <div style={{ textAlign: "right" }}>
              <strong className="pos">${wallet ? wallet.balance.toFixed(2) : "—"}</strong>
              {wallet?.live_error && (
                <div className="sub" style={{ color: "#ff7b7b", fontSize: "11px", marginTop: "2px" }}>
                  {wallet.live_error}
                </div>
              )}
            </div>
          </div>
          <div className="dashx-account-row">
            <span>Total P&L</span>
            <strong className={pnl >= 0 ? "pos" : "neg"}>{pnl >= 0 ? "+" : ""}${pnl.toFixed(4)}</strong>
          </div>
          <div className="dashx-account-row">
            <span>Bot</span>
            <strong>{bot?.status ?? "—"} · {bot?.state ?? "—"}</strong>
          </div>
          <div className="dashx-account-row">
            <span>Today</span>
            <strong>{openedToday} opened · {scannedToday} scanned</strong>
          </div>
        </div>
      </div>

      <div className="dashx-grid-mid">
        <div className="dashx-main">
          <div className="card dashx-chart-card dashx-chart-wide">
            <h3>Account Balance</h3>
            <div className="dashx-chart-wrap">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={balanceSeries}>
                  <CartesianGrid stroke="#1c2436" vertical={false} />
                  <XAxis dataKey="t" stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={{ background: "#101722", border: "1px solid #22314c" }} />
                  <Line type="linear" dataKey="balance" stroke="#2f9bff" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="card dashx-chart-card dashx-chart-wide">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <h3 style={{ margin: 0 }}>BTC Candles ({interval})</h3>
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn btn-secondary" disabled={interval === "5m"} onClick={() => setIntervalLabel("5m")}>5m</button>
                <button className="btn btn-secondary" disabled={interval === "15m"} onClick={() => setIntervalLabel("15m")}>15m</button>
              </div>
            </div>
            <div className="poly-candles">
              {candles.length === 0 && <div className="sub">No candle data.</div>}
              {candles.map((c) => {
                const up = c.close >= c.open;
                const bodyLow = Math.min(c.open, c.close);
                const bodyHigh = Math.max(c.open, c.close);
                const range = Math.max(0.000001, c.high - c.low);
                const wickTop = ((c.high - bodyHigh) / range) * 100;
                const wickBottom = ((bodyLow - c.low) / range) * 100;
                const bodyHeight = Math.max(1, ((bodyHigh - bodyLow) / range) * 100);
                return (
                  <div className="poly-candle-col" key={c.t}>
                    <div className={`poly-candle-body ${up ? "up" : "down"}`} style={{ marginTop: `${wickTop}%`, height: `${bodyHeight}%` }} />
                    <div className="poly-candle-wick" style={{ top: `${wickTop}%`, bottom: `${wickBottom}%` }} />
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        <div className="card dashx-side">
          <div className="dashx-side-tabs">
            <span>Closed</span>
            <span className="sub">Open: {summary?.open_count ?? openTrades.length}</span>
          </div>
          <div className="dashx-side-list">
            {closedTrades.length === 0 && <div className="sub">No recent closed trades.</div>}
            {closedTrades.slice(0, 50).map((t) => (
              <div className="dashx-side-item" key={t.id}>
                <div className="dashx-side-title">{t.market_title.slice(0, 60)}</div>
                <div className="dashx-side-sub">Crypto · {t.direction} · {t.status.replace("CLOSED_", "")}</div>
                <div className={t.pnl >= 0 ? "pos" : "neg"}>{t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(4)}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 10 }}>
        <h3 style={{ marginTop: 0 }}>Polymarket 5m/15m Focus Markets</h3>
        <table>
          <thead>
            <tr>
              <th>Market</th>
              <th>YES</th>
              <th>NO</th>
              <th>Vol</th>
              <th>Close In</th>
              <th>Live</th>
            </tr>
          </thead>
          <tbody>
            {markets.length === 0 && (
              <tr>
                <td colSpan={6} className="sub">No 5m/15m up/down markets found right now.</td>
              </tr>
            )}
            {markets.map((m) => {
              const evt = m.event_slug || m.slug;
              const href = evt ? `https://polymarket.com/event/${evt}` : "https://polymarket.com";
              const mins = Math.max(0, Math.round((m.time_to_close_seconds || 0) / 60));
              return (
                <tr key={m.id}>
                  <td title={m.title}>{m.title.slice(0, 80)}</td>
                  <td>{m.yes_price.toFixed(3)}</td>
                  <td>{m.no_price.toFixed(3)}</td>
                  <td>{Math.round(m.volume).toLocaleString()}</td>
                  <td>{mins}m</td>
                  <td><a href={href} target="_blank" rel="noreferrer">Open</a></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
