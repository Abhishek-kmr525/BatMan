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
  mode_guard?: { mode: string; kill_switch: boolean; live_enabled: boolean };
};

type KalshiWallet = {
  balance: number;
  total_pnl: number;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
};

type KalshiTrade = {
  id: string;
  market_title: string;
  direction: "YES" | "NO";
  entry_price: number;
  current_price?: number | null;
  amount: number;
  pnl: number;
  status: string;
};

type LogEntry = { id?: string; level: string; message: string; ts?: string; created_at?: string };

export default function KalshiPaperPage() {
  const [bot, setBot] = useState<KalshiBotStatus | null>(null);
  const [wallet, setWallet] = useState<KalshiWallet | null>(null);
  const [openTrades, setOpenTrades] = useState<KalshiTrade[]>([]);
  const [closedTrades, setClosedTrades] = useState<KalshiTrade[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [b, w, o, c, l] = await Promise.all([
      api<KalshiBotStatus>("/bot/status"),
      api<KalshiWallet>("/wallet"),
      api<KalshiTrade[]>("/trades?status=open&limit=20&page=1"),
      api<KalshiTrade[]>("/trades?status=closed&limit=20&page=1"),
      api<LogEntry[]>("/agent/logs?limit=60"),
    ]);
    setBot(b);
    setWallet(w);
    setOpenTrades(o);
    setClosedTrades(c);
    setLogs(l);
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, []);

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
  async function killToggle() {
    if (!bot?.mode_guard) return;
    setBusy(true);
    try {
      await api("/mode/kill-switch", {
        method: "POST",
        body: JSON.stringify({ platform: "kalshi", enabled: !bot.mode_guard.kill_switch }),
      });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  const running = bot?.status === "running";
  const pnlPos = (wallet?.total_pnl ?? 0) >= 0;

  return (
    <div className="container" style={{ maxWidth: "100%", padding: 16 }}>
      <div className="operator-header">
        <div className="operator-left">
          <div className="operator-title">AMTA COMMAND</div>
          <div className="operator-pill">Kalshi Bot Console [K1]</div>
          <div className="operator-pill">PAPER</div>
          <div className="operator-pill">HEALTHY</div>
        </div>
        <div className="operator-right">
          <button className="btn btn-secondary" onClick={() => refresh()} disabled={busy}>REFRESH</button>
          <button className="btn btn-start" onClick={startBot} disabled={busy}>START ENGINE</button>
          <button className="btn btn-stop" onClick={killToggle} disabled={busy}>KILL ALL</button>
        </div>
      </div>

      <section className="operator-kpis">
        <div className="operator-kpi"><span>WALLET BALANCE</span><strong>${(wallet?.balance ?? 0).toFixed(2)}</strong></div>
        <div className="operator-kpi"><span>TODAY PNL</span><strong className={pnlPos ? "pos" : "neg"}>{(wallet?.total_pnl ?? 0).toFixed(2)}</strong></div>
        <div className="operator-kpi"><span>OPEN POSITIONS</span><strong>{bot?.active_positions ?? 0}</strong></div>
        <div className="operator-kpi"><span>TRADES TODAY</span><strong>{bot?.trades_today ?? 0}</strong></div>
        <div className="operator-kpi"><span>WIN RATE</span><strong>{(wallet?.win_rate ?? 0).toFixed(1)}%</strong></div>
        <div className="operator-kpi"><span>SCAN RATE</span><strong>{bot?.last_scan_count ?? 0}</strong></div>
        <div className="operator-kpi"><span>STATE</span><strong className={running ? "pos" : "neg"}>{running ? "RUNNING" : "STOPPED"}</strong></div>
        <div className="operator-kpi"><span>KILL SWITCH</span><strong>{bot?.mode_guard?.kill_switch ? "ON" : "OFF"}</strong></div>
      </section>

      <div className="operator-main-grid">
        <div className="operator-card">
          <h3>Live Position Monitor</h3>
          <div className="operator-table-wrap">
            <table>
              <thead>
                <tr><th>Market</th><th>Side</th><th>Entry</th><th>Current</th><th>Size</th><th>PnL</th><th>Status</th></tr>
              </thead>
              <tbody>
                {openTrades.length === 0 && <tr><td colSpan={7} className="sub">No open Kalshi trades.</td></tr>}
                {openTrades.map((t) => (
                  <tr key={t.id}>
                    <td>{t.market_title}</td>
                    <td className={t.direction === "YES" ? "pos" : "neg"}>{t.direction}</td>
                    <td>{t.entry_price.toFixed(3)}</td>
                    <td>{(t.current_price ?? t.entry_price).toFixed(3)}</td>
                    <td>{t.amount.toFixed(2)}</td>
                    <td className={t.pnl >= 0 ? "pos" : "neg"}>{t.pnl.toFixed(4)}</td>
                    <td>{t.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="operator-card">
          <h3>Strategy & Risk</h3>
          <div className="operator-kv"><span>MODE</span><strong>{bot?.mode_guard?.mode || "paper"}</strong></div>
          <div className="operator-kv"><span>UPTIME</span><strong>{bot?.uptime_seconds ?? 0}s</strong></div>
          <div className="operator-kv"><span>SCAN TODAY</span><strong>{bot?.scanned_markets_today ?? 0}</strong></div>
          <div className="operator-kv"><span>CANDIDATES</span><strong>{bot?.last_candidate_count ?? 0}</strong></div>
          <div className="operator-kv"><span>KILL SWITCH</span><strong>{bot?.mode_guard?.kill_switch ? "ON" : "OFF"}</strong></div>
        </div>
      </div>

      <div className="operator-card" style={{ marginTop: 12 }}>
        <h3>Execution Feed Log Runner</h3>
        <div className="operator-log">
          {logs.map((l, i) => (
            <div key={`${l.id || i}`} className="operator-log-row">
              <span className="ts">{new Date(l.ts || l.created_at || Date.now()).toLocaleTimeString()}</span>
              <span className="platform">KALSHI</span>
              <span className={l.level === "ERROR" ? "neg" : "pos"}>[{l.level}]</span>
              <span className="msg">{l.message}</span>
              <span className="age"> </span>
            </div>
          ))}
        </div>
      </div>

      <div className="operator-card" style={{ marginTop: 12 }}>
        <h3>Recently Closed Trades</h3>
        <div className="operator-table-wrap">
          <table>
            <thead>
              <tr><th>Market</th><th>Side</th><th>Entry</th><th>Size</th><th>Result</th><th>Status</th></tr>
            </thead>
            <tbody>
              {closedTrades.length === 0 && <tr><td colSpan={6} className="sub">No closed trades.</td></tr>}
              {closedTrades.map((t) => (
                <tr key={t.id}>
                  <td>{t.market_title}</td>
                  <td>{t.direction}</td>
                  <td>{t.entry_price.toFixed(3)}</td>
                  <td>{t.amount.toFixed(2)}</td>
                  <td className={t.pnl >= 0 ? "pos" : "neg"}>{t.pnl.toFixed(4)}</td>
                  <td>{t.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <button className="btn btn-secondary" onClick={stopBot} disabled={busy}>STOP ENGINE</button>
      </div>
    </div>
  );
}
