"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";

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
  current_price?: number | null;
  pnl: number;
  status: string;
  agent_score: number;
  opened_at: string;
};

export default function PolymarketPage() {
  const [bot, setBot] = useState<PolyBotStatus | null>(null);
  const [wallet, setWallet] = useState<PolyWallet | null>(null);
  const [summary, setSummary] = useState<PolySummary | null>(null);
  const [openTrades, setOpenTrades] = useState<PolyTrade[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const [b, w, s, o] = await Promise.all([
        api<PolyBotStatus>("/polymarket/bot/status"),
        api<PolyWallet>("/polymarket/wallet"),
        api<PolySummary>("/polymarket/trades/summary"),
        api<PolyTrade[]>("/polymarket/trades?status=open&limit=30&page=1"),
      ]);
      setBot(b);
      setWallet(w);
      setSummary(s);
      setOpenTrades(o);
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

  return (
    <div className="container">
      <h1>Polymarket Bot (Paper Mode)</h1>
      <p className="sub">
        Phase-1: separate paper wallet and separate bot loop (no live trading yet).
      </p>
      {error && <p className="sub" style={{ color: "#ff7b7b" }}>Live refresh issue: {error}</p>}

      <div style={{ display: "flex", gap: 10, marginBottom: 12 }}>
        <button className="btn btn-start" disabled={busy} onClick={startBot}>Start Bot</button>
        <button className="btn btn-stop" disabled={busy} onClick={stopBot}>Stop Bot</button>
        <button className="btn btn-secondary" disabled={busy} onClick={resetPaper}>Reset Paper ($20)</button>
      </div>

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="row"><span>Status</span><strong>{bot?.status ?? "—"} · {bot?.state ?? "—"}</strong></div>
        <div className="row"><span>Wallet</span><strong>${wallet?.balance?.toFixed(2) ?? "—"}</strong></div>
        <div className="row"><span>Today</span><strong>{summary?.today_opened_count ?? 0} opened · {bot?.scanned_markets_today ?? 0} scanned</strong></div>
        <div className="row"><span>Candidates</span><strong>last scan {bot?.last_candidate_count ?? 0} / {bot?.last_scan_count ?? 0}</strong></div>
        <div className="row"><span>Open Positions</span><strong>{summary?.open_count ?? 0} / {bot?.max_concurrent_positions ?? 0}</strong></div>
      </div>

      <h2>Open Paper Trades</h2>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Market</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Score</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {openTrades.length === 0 && (
              <tr>
                <td colSpan={5} className="sub">No open polymarket trades yet.</td>
              </tr>
            )}
            {openTrades.map((t) => (
              <tr key={t.id}>
                <td>{t.market_title}</td>
                <td>{t.direction}</td>
                <td>{t.entry_price.toFixed(2)}</td>
                <td>{t.agent_score}</td>
                <td>{t.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

