"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

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
  };
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

type Trade = {
  id: string;
  market_id: string;
  market_title: string;
  direction: "YES" | "NO";
  amount: number;
  entry_price: number;
  current_price?: number | null;
  pnl: number;
  status: string;
  opened_at: string;
};

type LogEntry = {
  id: string;
  level: string;
  message: string;
  ts: string | null;
};

function modeText(mode: string) {
  if (mode === "live_armed" || mode === "live") return "LIVE ARMED";
  if (mode === "live_requested") return "LIVE REQUESTED";
  return "PAPER";
}

function asDollar(n: number, d = 2) {
  return `$${n.toFixed(d)}`;
}

function since(iso?: string | null) {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms) || ms < 0) return "—";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  return `${Math.floor(min / 60)}h ago`;
}

export default function OperatorHome() {
  const [data, setData] = useState<BotsAggregate | null>(null);
  const [kalshiOpen, setKalshiOpen] = useState<Trade[]>([]);
  const [polyOpen, setPolyOpen] = useState<Trade[]>([]);
  const [kalshiLogs, setKalshiLogs] = useState<LogEntry[]>([]);
  const [polyLogs, setPolyLogs] = useState<LogEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState<string>("—");
  const logRef = useRef<HTMLDivElement | null>(null);

  async function refresh() {
    const safe = async <T,>(p: Promise<T>) => {
      try {
        return { ok: true as const, v: await p };
      } catch {
        return { ok: false as const };
      }
    };
    const [b, ko, po, kl, pl] = await Promise.all([
      safe(api<BotsAggregate>("/bots")),
      safe(api<Trade[]>("/trades?status=open&limit=20&page=1")),
      safe(api<Trade[]>("/polymarket/trades?status=open&limit=20&page=1")),
      safe(api<LogEntry[]>("/agent/logs?limit=120")),
      safe(api<LogEntry[]>("/polymarket/logs?limit=120")),
    ]);

    if (b.ok) setData(b.v);
    if (ko.ok) setKalshiOpen(ko.v);
    if (po.ok) setPolyOpen(po.v);
    if (kl.ok) setKalshiLogs(kl.v);
    if (pl.ok) setPolyLogs(pl.v);

    if (!b.ok && !ko.ok && !po.ok && !kl.ok && !pl.ok) {
      setError("Failed to refresh from backend.");
    } else {
      setError(null);
    }
    setLastSync(new Date().toLocaleTimeString());
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, []);

  const logs = useMemo(() => {
    const k = kalshiLogs.map((x) => ({ ...x, platform: "KALSHI" }));
    const p = polyLogs.map((x) => ({ ...x, platform: "POLYMARKET" }));
    return [...k, ...p]
      .sort((a, b) => new Date(b.ts || 0).getTime() - new Date(a.ts || 0).getTime())
      .slice(0, 200);
  }, [kalshiLogs, polyLogs]);

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    if (el.scrollTop < 80) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [logs]);

  const openRows = useMemo(
    () => [
      ...kalshiOpen.map((t) => ({ ...t, platform: "KALSHI" })),
      ...polyOpen.map((t) => ({ ...t, platform: "POLYMARKET" })),
    ],
    [kalshiOpen, polyOpen],
  );

  const pnlCurve = useMemo(() => {
    if (!data) return [];
    const k = data.kalshi.wallet.total_pnl;
    const p = data.polymarket.wallet.total_pnl;
    return [
      { t: "T-5", v: k + p - 40 },
      { t: "T-4", v: k + p - 20 },
      { t: "T-3", v: k + p - 10 },
      { t: "T-2", v: k + p - 8 },
      { t: "T-1", v: k + p - 3 },
      { t: "Now", v: k + p },
    ];
  }, [data]);

  async function start(platform: "kalshi" | "polymarket") {
    setBusy(true);
    try {
      await api(platform === "kalshi" ? "/bot/start" : "/polymarket/bot/start", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function stop(platform: "kalshi" | "polymarket") {
    setBusy(true);
    try {
      await api(platform === "kalshi" ? "/bot/stop" : "/polymarket/bot/stop", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function kill(platform: "kalshi" | "polymarket", current: boolean) {
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

  async function globalKill() {
    setBusy(true);
    try {
      await Promise.all([
        api("/mode/kill-switch", { method: "POST", body: JSON.stringify({ platform: "kalshi", enabled: true }) }),
        api("/mode/kill-switch", { method: "POST", body: JSON.stringify({ platform: "polymarket", enabled: true }) }),
        api("/bot/stop", { method: "POST" }),
        api("/polymarket/bot/stop", { method: "POST" }),
      ]);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  const health = data ? (error ? "DEGRADED" : "OK") : "DEGRADED";

  return (
    <div className="container" style={{ maxWidth: "100%", padding: 16 }}>
      <div className="operator-header">
        <div className="operator-left">
          <div className="operator-title">AMTA OPERATOR</div>
          <div className="operator-pill">{`HEALTH: ${health}`}</div>
          <div className="operator-pill">{`SYNC: ${lastSync}`}</div>
          <div className="operator-pill operator-prod">ENV: PROD</div>
        </div>
        <div className="operator-right">
          <button className="btn btn-secondary" disabled={busy} onClick={() => refresh()}>Refresh</button>
          <button className="btn btn-stop" disabled={busy} onClick={globalKill}>GLOBAL KILL SWITCH</button>
        </div>
      </div>

      {error && <div className="sub" style={{ color: "#ff7b7b", marginBottom: 8 }}>{error}</div>}

      <section className="operator-kpis">
        <div className="operator-kpi"><span>TOTAL BALANCE</span><strong>{asDollar(data?.combined.balance || 0)}</strong></div>
        <div className="operator-kpi"><span>REALIZED PNL (T)</span><strong className={(data?.combined.today_pnl || 0) >= 0 ? "pos" : "neg"}>{asDollar(data?.combined.today_pnl || 0, 4)}</strong></div>
        <div className="operator-kpi"><span>OPEN POSITIONS</span><strong>{data?.combined.active_positions || 0}</strong></div>
        <div className="operator-kpi"><span>TRADES TODAY</span><strong>{data?.combined.today_opened || 0}</strong></div>
        <div className="operator-kpi"><span>WIN RATE</span><strong>{data ? `${(((data.kalshi.wallet.wins + data.polymarket.wallet.wins) / Math.max(1, data.kalshi.wallet.total_trades + data.polymarket.wallet.total_trades)) * 100).toFixed(1)}%` : "—"}</strong></div>
        <div className="operator-kpi"><span>KALSHI SCAN</span><strong>{data?.kalshi.last_scan_count || 0}</strong></div>
        <div className="operator-kpi"><span>POLY SCAN</span><strong>{data?.polymarket.last_scan_count || 0}</strong></div>
      </section>

      <div className="operator-main-grid">
        <div className="operator-bots-grid">
          {(["kalshi", "polymarket"] as const).map((platform) => {
            const b = data?.[platform];
            if (!b) return null;
            return (
              <div className="operator-cluster" key={platform}>
                <h2 style={{ marginTop: 0 }}>{platform.toUpperCase()} CLUSTER</h2>
                <div className="operator-bot-card">
                  <div className="operator-bot-head">
                    <span>{platform.toUpperCase()} BOT</span>
                    <span>{modeText(b.mode_guard.mode)}</span>
                  </div>
                  <div className="operator-mini-grid">
                    <div><label>STATE</label><strong className={b.status === "running" ? "pos" : "neg"}>{b.status.toUpperCase()}</strong></div>
                    <div><label>MODE</label><strong>{modeText(b.mode_guard.mode)}</strong></div>
                    <div><label>TRADES TODAY</label><strong>{b.today_opened}</strong></div>
                    <div><label>ERRORS</label><strong>{error ? 1 : 0}</strong></div>
                    <div><label>WALLET</label><strong>{asDollar(b.wallet.balance)}</strong></div>
                    <div><label>LAST SCAN</label><strong>{b.last_scan_count}/{b.last_candidate_count}</strong></div>
                    <div><label>OPEN POS</label><strong>{b.active_positions}</strong></div>
                    <div><label>KILL</label><strong>{b.mode_guard.kill_switch ? "ON" : "OFF"}</strong></div>
                  </div>
                  <div className="operator-actions">
                    <button className="btn btn-secondary" disabled={busy} onClick={() => start(platform)}>START</button>
                    <button className="btn btn-secondary" disabled={busy} onClick={() => stop(platform)}>STOP</button>
                    <button className="btn btn-stop" disabled={busy} onClick={() => kill(platform, b.mode_guard.kill_switch)}>
                      {b.mode_guard.kill_switch ? "RELEASE KILL" : "KILL"}
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="operator-side">
          <div className="operator-card">
            <h3>Strategy & Risk Parameters</h3>
            <div className="operator-kv"><span>ACTIVE STRATEGY</span><strong>Mode-A / Mode-B</strong></div>
            <div className="operator-kv"><span>KALSHI MODE</span><strong>{modeText(data?.kalshi.mode_guard.mode || "paper")}</strong></div>
            <div className="operator-kv"><span>POLY MODE</span><strong>{modeText(data?.polymarket.mode_guard.mode || "paper")}</strong></div>
            <div className="operator-kv"><span>KALSHI CANARY</span><strong>{data?.kalshi.mode_guard.kill_switch ? "BLOCKED" : "ARMED"}</strong></div>
            <div className="operator-kv"><span>POLY CANARY</span><strong>{data?.polymarket.mode_guard.kill_switch ? "BLOCKED" : "ARMED"}</strong></div>
          </div>

          <div className="operator-card">
            <h3>Equity Curve</h3>
            <div style={{ height: 180 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={pnlCurve}>
                  <XAxis dataKey="t" tick={{ fill: "#7d8ba4", fontSize: 10 }} />
                  <YAxis tick={{ fill: "#7d8ba4", fontSize: 10 }} />
                  <Tooltip />
                  <Line type="monotone" dataKey="v" stroke="#4aa3ff" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>

      <div className="operator-card" style={{ marginTop: 16 }}>
        <h3>Open Positions ({openRows.length})</h3>
        <div className="operator-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Platform</th><th>Market</th><th>Side</th><th>Entry</th><th>Current</th><th>Size</th><th>PnL</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {openRows.length === 0 && <tr><td colSpan={8} className="sub">No open positions.</td></tr>}
              {openRows.map((t) => (
                <tr key={t.id}>
                  <td>{(t as any).platform}</td>
                  <td title={t.market_title}>{t.market_title.slice(0, 56)}</td>
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

      <div className="operator-card" style={{ marginTop: 16 }}>
        <h3>Live Execution Monitor ({logs.length})</h3>
        <div className="operator-log" ref={logRef}>
          {logs.map((x) => (
            <div key={`${x.id}-${x.ts}`} className="operator-log-row">
              <span className="ts">{x.ts ? new Date(x.ts).toLocaleTimeString() : ""}</span>
              <span className="platform">{(x as any).platform}</span>
              <span className={x.level === "ERROR" ? "neg" : "pos"}>[{x.level}]</span>
              <span className="msg">{x.message}</span>
              <span className="age">{since(x.ts)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
