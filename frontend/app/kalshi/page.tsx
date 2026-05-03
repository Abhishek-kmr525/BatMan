"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";

type KalshiBotStatus = {
  status: string;
  state: string;
  uptime_seconds: number;
  trades_today?: number;
  scanned_markets_today?: number;
  last_scan_count?: number;
  last_candidate_count?: number;
  active_positions: number;
  max_concurrent_positions: number;
  mode_guard?: { mode: "paper" | "live"; kill_switch: boolean };
};

type KalshiWallet = {
  balance: number;
  total_pnl: number;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
};

type KalshiSummary = {
  total_count?: number;
  open_count?: number;
  closed_count?: number;
  today_opened_count?: number;
  today_closed_count?: number;
};

type KalshiTrade = {
  id: string;
  market_id: string;
  market_title: string;
  direction: "YES" | "NO";
  amount: number;
  entry_price: number;
  pnl: number;
  status: string;
  agent_score: number;
  opened_at: string;
};

export default function KalshiPage() {
  const [bot, setBot] = useState<KalshiBotStatus | null>(null);
  const [wallet, setWallet] = useState<KalshiWallet | null>(null);
  const [summary, setSummary] = useState<KalshiSummary | null>(null);
  const [openTrades, setOpenTrades] = useState<KalshiTrade[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const [b, w, s, o] = await Promise.all([
        api<KalshiBotStatus>("/bot/status"),
        api<KalshiWallet>("/wallet"),
        api<KalshiSummary>("/trades/summary"),
        api<KalshiTrade[]>("/trades?status=open&limit=30&page=1"),
      ]);
      setBot(b);
      setWallet(w);
      setSummary(s);
      setOpenTrades(Array.isArray(o) ? o : (o as any).items ?? []);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "refresh failed");
    }
  }

  async function startBot() {
    setBusy(true);
    try {
      await api("/bot/start", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function stopBot() {
    setBusy(true);
    try {
      await api("/bot/stop", { method: "POST" });
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

  const live = bot?.mode_guard?.mode === "live";

  return (
    <div className="container">
      <h1>
        Kalshi Bot
        <span style={{
          marginLeft: 8,
          fontSize: 12,
          padding: "2px 8px",
          borderRadius: 6,
          background: live ? "#7f1d1d" : "#1f2937",
          color: live ? "#fecaca" : "#9ca3af",
          verticalAlign: "middle",
        }}>
          {live ? "LIVE (ARMED)" : "PAPER"}
        </span>
      </h1>
      <p className="sub">Kalshi-only view. Use the Bots Control Center for arming and kill switch.</p>
      {error && <p className="sub" style={{ color: "#ff7b7b" }}>Live refresh issue: {error}</p>}

      <div style={{ display: "flex", gap: 10, marginBottom: 12 }}>
        <button className="btn btn-start" disabled={busy} onClick={startBot}>Start Bot</button>
        <button className="btn btn-stop" disabled={busy} onClick={stopBot}>Stop Bot</button>
      </div>

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="row"><span>Status</span><strong>{bot?.status ?? "—"} · {bot?.state ?? "—"}</strong></div>
        <div className="row"><span>Wallet</span><strong>${wallet?.balance?.toFixed(2) ?? "—"}</strong></div>
        <div className="row"><span>Total PnL</span>
          <strong style={{ color: (wallet?.total_pnl ?? 0) >= 0 ? "#4ade80" : "#ff7b7b" }}>
            ${wallet?.total_pnl?.toFixed(4) ?? "—"}
          </strong>
        </div>
        <div className="row"><span>Win rate</span>
          <strong>{wallet?.win_rate?.toFixed(1) ?? "—"}% ({wallet?.wins ?? 0}W/{wallet?.losses ?? 0}L)</strong>
        </div>
        <div className="row"><span>Today</span>
          <strong>{summary?.today_opened_count ?? 0} opened · {bot?.scanned_markets_today ?? 0} scanned</strong>
        </div>
        <div className="row"><span>Candidates</span>
          <strong>last scan {bot?.last_candidate_count ?? 0} / {bot?.last_scan_count ?? 0}</strong>
        </div>
        <div className="row"><span>Open Positions</span>
          <strong>{summary?.open_count ?? 0} / {bot?.max_concurrent_positions ?? 0}</strong>
        </div>
      </div>

      <h2>Open Trades</h2>
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
              <tr><td colSpan={5} className="sub">No open Kalshi trades.</td></tr>
            )}
            {openTrades.map((t) => (
              <tr key={t.id}>
                <td>{t.market_title}</td>
                <td>{t.direction}</td>
                <td>{t.entry_price?.toFixed(2)}</td>
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
