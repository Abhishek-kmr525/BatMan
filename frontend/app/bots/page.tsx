"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";

type BotSnapshot = {
  platform: string;
  status: string;
  state: string;
  uptime_seconds: number;
  scanned_today: number;
  last_scan_count: number;
  last_candidate_count: number;
  active_positions: number;
  max_concurrent_positions: number;
  today_opened: number;
  today_pnl: number;
  wallet: {
    balance: number;
    total_pnl: number;
    total_trades: number;
    wins: number;
    losses: number;
  };
  mode_guard: {
    platform: string;
    mode: string;
    live_enabled: boolean;
    kill_switch: boolean;
    requested_at?: string | null;
    armed_at?: string | null;
  };
};

type BotsAggregate = {
  kalshi: BotSnapshot;
};

type Trade = {
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

function modeBadge(mode: string) {
  if (mode === "live_armed" || mode === "live") return "LIVE ARMED";
  if (mode === "live_requested") return "LIVE REQUESTED";
  return "PAPER";
}

export default function KalshiLivePage() {
  const [k, setK] = useState<BotSnapshot | null>(null);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [closedTrades, setClosedTrades] = useState<Trade[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [agg, o, c, l] = await Promise.all([
      api<BotsAggregate>("/bots"),
      api<Trade[]>("/trades?status=open&limit=20&page=1"),
      api<Trade[]>("/trades?status=closed&limit=20&page=1"),
      api<LogEntry[]>("/agent/logs?limit=80"),
    ]);
    setK(agg.kalshi);
    setOpenTrades(o);
    setClosedTrades(c);
    setLogs(l);
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, []);

  async function start() {
    setBusy(true);
    try {
      await api("/bot/start", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  async function stop() {
    setBusy(true);
    try {
      await api("/bot/stop", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  async function killToggle() {
    if (!k) return;
    setBusy(true);
    try {
      await api("/mode/kill-switch", {
        method: "POST",
        body: JSON.stringify({ platform: "kalshi", enabled: !k.mode_guard.kill_switch }),
      });
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  async function armOrPaper() {
    if (!k) return;
    setBusy(true);
    try {
      if (k.mode_guard.mode === "live_armed") {
        await api("/mode/set-paper", { method: "POST", body: JSON.stringify({ platform: "kalshi" }) });
      } else if (k.mode_guard.mode === "live_requested") {
        const pass = prompt("Enter live mode passcode:");
        if (!pass) return;
        await api("/mode/confirm-live", {
          method: "POST",
          body: JSON.stringify({ platform: "kalshi", passcode: pass }),
        });
      } else {
        await api("/mode/request-live", { method: "POST", body: JSON.stringify({ platform: "kalshi" }) });
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  const live = k?.mode_guard.mode === "live_armed" || k?.mode_guard.mode === "live";
  const running = k?.status === "running";

  return (
    <div className="container" style={{ maxWidth: "100%", padding: 16 }}>
      <div className="operator-header">
        <div className="operator-left">
          <div className="operator-title">AMTA COMMAND</div>
          <div className="operator-pill">Kalshi Bot Console [K2]</div>
          <div className="operator-pill">{modeBadge(k?.mode_guard.mode || "paper")}</div>
          <div className="operator-pill">HEALTHY</div>
        </div>
        <div className="operator-right">
          <button className="btn btn-secondary" onClick={() => refresh()} disabled={busy}>REFRESH</button>
          <button className="btn btn-start" onClick={start} disabled={busy}>START ENGINE</button>
          <button className="btn btn-stop" onClick={killToggle} disabled={busy}>KILL ALL</button>
        </div>
      </div>

      <section className="operator-kpis">
        <div className="operator-kpi"><span>WALLET BALANCE</span><strong>${(k?.wallet.balance ?? 0).toFixed(2)}</strong></div>
        <div className="operator-kpi"><span>TODAY PNL</span><strong className={(k?.today_pnl ?? 0) >= 0 ? "pos" : "neg"}>{(k?.today_pnl ?? 0).toFixed(2)}</strong></div>
        <div className="operator-kpi"><span>7D PNL</span><strong>{(k?.wallet.total_pnl ?? 0).toFixed(2)}</strong></div>
        <div className="operator-kpi"><span>OPEN POSITIONS</span><strong>{k?.active_positions ?? 0}</strong></div>
        <div className="operator-kpi"><span>TRADES TODAY</span><strong>{k?.today_opened ?? 0}</strong></div>
        <div className="operator-kpi"><span>WIN RATE</span><strong>{k ? ((k.wallet.wins / Math.max(1, k.wallet.total_trades)) * 100).toFixed(1) : "0.0"}%</strong></div>
        <div className="operator-kpi"><span>SCAN RATE</span><strong>{k?.last_scan_count ?? 0}</strong></div>
        <div className="operator-kpi"><span>ERRORS</span><strong>0</strong></div>
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
          <div className="operator-kv"><span>MODE</span><strong>{modeBadge(k?.mode_guard.mode || "paper")}</strong></div>
          <div className="operator-kv"><span>STATE</span><strong className={running ? "pos" : "neg"}>{running ? "RUNNING" : "STOPPED"}</strong></div>
          <div className="operator-kv"><span>UPTIME</span><strong>{k?.uptime_seconds ?? 0}s</strong></div>
          <div className="operator-kv"><span>SCAN TODAY</span><strong>{k?.scanned_today ?? 0}</strong></div>
          <div className="operator-kv"><span>KILL SWITCH</span><strong>{k?.mode_guard.kill_switch ? "ON" : "OFF"}</strong></div>
          <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
            <button className="btn btn-secondary" onClick={armOrPaper} disabled={busy || !k?.mode_guard.live_enabled}>
              {live ? "BACK TO PAPER" : k?.mode_guard.mode === "live_requested" ? "CONFIRM LIVE" : "REQUEST LIVE"}
            </button>
            <button className="btn btn-secondary" onClick={stop} disabled={busy}>STOP</button>
          </div>
        </div>
      </div>

      <div className="operator-card" style={{ marginTop: 12 }}>
        <h3>Execution Log Runner</h3>
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
    </div>
  );
}
