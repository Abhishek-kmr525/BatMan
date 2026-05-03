"use client";

import { useEffect, useState } from "react";
import { api, API } from "../../lib/api";

type ModeGuard = {
  mode: "paper" | "live";
  pending_live: boolean;
  kill_switch: boolean;
  live_enabled: boolean;
  limits?: Record<string, any>;
};

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
  mode_guard: ModeGuard;
};

type BotsAggregate = {
  kalshi: BotSnapshot;
  polymarket: BotSnapshot;
  combined: {
    today_pnl: number;
    today_opened: number;
    active_positions: number;
    balance: number;
  };
};

type DailyRow = { date: string; pnl: number; trades: number; wins: number; losses: number };
type DailyReport = {
  from: string;
  to: string;
  days: number;
  series: { kalshi?: DailyRow[]; polymarket?: DailyRow[] };
  combined: DailyRow[];
};

const startPaths: Record<string, string> = {
  kalshi: "/bot/start",
  polymarket: "/polymarket/bot/start",
};
const stopPaths: Record<string, string> = {
  kalshi: "/bot/stop",
  polymarket: "/polymarket/bot/stop",
};

export default function BotsPage() {
  const [data, setData] = useState<BotsAggregate | null>(null);
  const [report, setReport] = useState<DailyReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const [agg, rep] = await Promise.all([
        api<BotsAggregate>("/bots"),
        api<DailyReport>("/reports/daily?days=14"),
      ]);
      setData(agg);
      setReport(rep);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "refresh failed");
    }
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, []);

  async function start(platform: string) {
    setBusy(true);
    try {
      await api(startPaths[platform], { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function stop(platform: string) {
    setBusy(true);
    try {
      await api(stopPaths[platform], { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function requestLive(platform: string) {
    setBusy(true);
    try {
      await api("/mode/request-live", {
        method: "POST",
        body: JSON.stringify({ platform }),
      });
      await refresh();
    } catch (e: any) {
      alert(e?.message || "request failed");
    } finally {
      setBusy(false);
    }
  }

  async function confirmLive(platform: string) {
    const pass = prompt(`Confirm LIVE mode for ${platform} — passcode`, "");
    if (!pass) return;
    setBusy(true);
    try {
      await api("/mode/confirm-live", {
        method: "POST",
        body: JSON.stringify({ platform, passcode: pass }),
      });
      await refresh();
    } catch (e: any) {
      alert(e?.message || "confirm failed");
    } finally {
      setBusy(false);
    }
  }

  async function setPaper(platform: string) {
    setBusy(true);
    try {
      await api("/mode/set-paper", {
        method: "POST",
        body: JSON.stringify({ platform }),
      });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function toggleKill(platform: string, current: boolean) {
    setBusy(true);
    try {
      await api("/mode/kill-switch", {
        method: "POST",
        body: JSON.stringify({ platform, enabled: !current }),
      });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="container">
      <h1>Bots Control Center</h1>
      <p className="sub">Live view of both bots, mode arming, kill switch, and 14-day PnL.</p>
      {error && <p className="sub" style={{ color: "#ff7b7b" }}>Refresh issue: {error}</p>}

      {data && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div className="row">
            <span>Combined balance</span>
            <strong>${data.combined.balance.toFixed(2)}</strong>
          </div>
          <div className="row">
            <span>Today PnL</span>
            <strong style={{ color: data.combined.today_pnl >= 0 ? "#4ade80" : "#ff7b7b" }}>
              ${data.combined.today_pnl.toFixed(4)}
            </strong>
          </div>
          <div className="row">
            <span>Today opened</span>
            <strong>{data.combined.today_opened}</strong>
          </div>
          <div className="row">
            <span>Active positions</span>
            <strong>{data.combined.active_positions}</strong>
          </div>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 12 }}>
        {data && (["kalshi", "polymarket"] as const).map((p) => {
          const b = data[p];
          const live = b.mode_guard.mode === "live";
          return (
            <div key={p} className="card">
              <h2 style={{ textTransform: "capitalize", marginTop: 0 }}>
                {p}
                <span style={{
                  marginLeft: 8,
                  fontSize: 12,
                  padding: "2px 8px",
                  borderRadius: 6,
                  background: live ? "#7f1d1d" : "#1f2937",
                  color: live ? "#fecaca" : "#9ca3af",
                }}>
                  {live ? "LIVE (ARMED)" : "PAPER"}
                </span>
                {b.mode_guard.kill_switch && (
                  <span style={{ marginLeft: 6, fontSize: 12, color: "#ff7b7b" }}>KILL</span>
                )}
              </h2>
              <div className="row"><span>Status</span><strong>{b.status} · {b.state}</strong></div>
              <div className="row"><span>Wallet</span><strong>${b.wallet.balance.toFixed(2)}</strong></div>
              <div className="row"><span>Today PnL</span>
                <strong style={{ color: b.today_pnl >= 0 ? "#4ade80" : "#ff7b7b" }}>
                  ${b.today_pnl.toFixed(4)}
                </strong>
              </div>
              <div className="row"><span>Today opened</span><strong>{b.today_opened}</strong></div>
              <div className="row"><span>Open positions</span>
                <strong>{b.active_positions} / {b.max_concurrent_positions}</strong>
              </div>
              <div className="row"><span>Last scan</span>
                <strong>{b.last_candidate_count}/{b.last_scan_count}</strong>
              </div>
              <div className="row"><span>Scanned today</span><strong>{b.scanned_today}</strong></div>
              <div className="row"><span>Total trades</span>
                <strong>{b.wallet.total_trades} ({b.wallet.wins}W/{b.wallet.losses}L)</strong>
              </div>

              <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
                <button className="btn btn-start" disabled={busy} onClick={() => start(p)}>Start</button>
                <button className="btn btn-stop" disabled={busy} onClick={() => stop(p)}>Stop</button>
                {!live ? (
                  <>
                    <button className="btn btn-secondary" disabled={busy || !b.mode_guard.live_enabled}
                      onClick={() => (b.mode_guard.pending_live ? confirmLive(p) : requestLive(p))}>
                      {b.mode_guard.pending_live ? "Confirm LIVE" : "Request LIVE"}
                    </button>
                  </>
                ) : (
                  <button className="btn btn-secondary" disabled={busy} onClick={() => setPaper(p)}>Back to Paper</button>
                )}
                <button className="btn btn-secondary" disabled={busy} onClick={() => toggleKill(p, b.mode_guard.kill_switch)}>
                  {b.mode_guard.kill_switch ? "Release Kill" : "Kill Switch"}
                </button>
              </div>
              {!b.mode_guard.live_enabled && (
                <p className="sub" style={{ marginTop: 8 }}>
                  Live disabled in env (set {p.toUpperCase()}_LIVE_ENABLED=true to allow arming).
                </p>
              )}
            </div>
          );
        })}
      </div>

      <h2 style={{ marginTop: 24 }}>14-day PnL</h2>
      <div className="card">
        <div style={{ display: "flex", gap: 10, marginBottom: 10 }}>
          <a className="btn btn-secondary" href={`${API}/api/reports/export.csv?platform=all&status=all`}>
            Export all CSV
          </a>
          <a className="btn btn-secondary" href={`${API}/api/reports/export.csv?platform=kalshi&status=all`}>
            Export Kalshi
          </a>
          <a className="btn btn-secondary" href={`${API}/api/reports/export.csv?platform=polymarket&status=all`}>
            Export Polymarket
          </a>
        </div>
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Kalshi PnL</th>
              <th>Polymarket PnL</th>
              <th>Combined PnL</th>
              <th>Trades</th>
            </tr>
          </thead>
          <tbody>
            {report && report.combined.length === 0 && (
              <tr><td colSpan={5} className="sub">No closed trades in the window.</td></tr>
            )}
            {report?.combined.map((row, i) => {
              const k = report.series.kalshi?.[i]?.pnl ?? 0;
              const pm = report.series.polymarket?.[i]?.pnl ?? 0;
              return (
                <tr key={row.date}>
                  <td>{row.date}</td>
                  <td style={{ color: k >= 0 ? "#4ade80" : "#ff7b7b" }}>${k.toFixed(4)}</td>
                  <td style={{ color: pm >= 0 ? "#4ade80" : "#ff7b7b" }}>${pm.toFixed(4)}</td>
                  <td style={{ color: row.pnl >= 0 ? "#4ade80" : "#ff7b7b" }}>${row.pnl.toFixed(4)}</td>
                  <td>{row.trades}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
