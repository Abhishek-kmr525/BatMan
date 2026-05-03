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
};

type PolyWallet = {
  balance: number;
  total_pnl: number;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
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

type RangeLabel = "This Week" | "This Month" | "This Year" | "All Time";

function inDays(d: Date, n: number) {
  return Date.now() - d.getTime() <= n * 24 * 60 * 60 * 1000;
}

function fmtCurrency(n: number) {
  return `${n >= 0 ? "+" : "-"}$${Math.abs(n).toFixed(2)}`;
}

function statForRange(label: RangeLabel, closed: PolyTrade[]) {
  const filtered = closed.filter((t) => {
    const dt = new Date(t.closed_at || t.opened_at);
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

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, []);

  const ranges: RangeLabel[] = ["This Week", "This Month", "This Year", "All Time"];
  const stats = ranges.map((r) => statForRange(r, closedTrades));

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
            <strong className="pos">${wallet ? wallet.balance.toFixed(2) : "—"}</strong>
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
    </div>
  );
}
