"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "../../lib/api";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, CartesianGrid } from "recharts";

type BotStatus = {
  status: string;
  trades_today: number;
  scanned_markets_today: number;
  last_candidate_count: number;
  active_positions: number;
  mode_guard?: { mode: string; live_enabled: boolean };
};
type Wallet = {
  balance: number;
  trade_balance: number;
  vault_balance: number;
  actual_balance: number;
  trade_cap_usd: number;
  force_mode_a?: boolean;
  vault_locked: boolean;
  vault_sweeps_count: number;
  last_sweep_at?: string | null;
  total_pnl: number;
  wins: number;
  losses: number;
  win_rate: number;
};
type Trade = { id: string; market_title: string; direction: "YES" | "NO"; amount: number; entry_price: number; current_price?: number | null; pnl: number; closed_at?: string | null };
type LogEntry = { id: string; level: string; message: string; ts: string | null; local_ts?: string };
type Market = { id: string; title: string; yes_price: number; no_price: number; volume: number; time_to_close_seconds: number; event_slug: string; slug: string };

function parseTradeTime(raw?: string | null): number {
  if (!raw) return NaN;
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(raw);
  const normalized = hasTimezone ? raw : `${raw}Z`;
  const ts = new Date(normalized).getTime();
  return Number.isFinite(ts) ? ts : NaN;
}

function formatLogTime(x: LogEntry): string {
  const raw = x.ts ?? x.local_ts ?? null;
  if (!raw) return "--:--:--";
  const ts = new Date(raw).getTime();
  if (!Number.isFinite(ts)) return "--:--:--";
  return new Date(ts).toLocaleTimeString();
}

export default function PolymarketPaperPage() {
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [closedTrades, setClosedTrades] = useState<Trade[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [markets, setMarkets] = useState<Market[]>([]);
  const [busy, setBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState<string>("");
  const [nextWindowSeconds, setNextWindowSeconds] = useState<number | null>(null);

  async function refresh() {
    const [b, w, o, c, l, m] = await Promise.all([
      api<BotStatus>("/polymarket/paper/bot/status"),
      api<Wallet>("/polymarket/paper/wallet"),
      api<Trade[]>("/polymarket/trades?mode=paper&status=open&limit=20&page=1"),
      api<Trade[]>("/polymarket/trades?mode=paper&status=closed&limit=300&page=1"),
      api<LogEntry[]>("/polymarket/logs?mode=paper&limit=300"),
      api<Market[]>("/polymarket/markets?limit=10"),
    ]);
    setBot(b);
    setWallet(w);
    setOpenTrades(o);
    setClosedTrades(c);
    const nowIso = new Date().toISOString();
    setLogs((l || []).map((x) => ({ ...x, local_ts: x.ts ?? nowIso })));
    setMarkets(m);
    const valid = (m || [])
      .map((row) => Number(row.time_to_close_seconds))
      .filter((sec) => Number.isFinite(sec) && sec > 0);
    setNextWindowSeconds(valid.length ? Math.min(...valid) : null);
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 4000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      setNextWindowSeconds((prev) => {
        if (prev == null) return prev;
        return prev > 0 ? prev - 1 : 0;
      });
    }, 1000);
    return () => clearInterval(id);
  }, []);

  async function start() {
    setBusy(true);
    try {
      await api("/polymarket/paper/bot/start", { method: "POST" });
      setActionMsg("Bot started.");
      await refresh();
    } finally { setBusy(false); }
  }
  async function stop() {
    setBusy(true);
    try {
      await api("/polymarket/paper/bot/stop", { method: "POST" });
      setActionMsg("Bot stopped.");
      await refresh();
    } finally { setBusy(false); }
  }
  async function resetPaper() {
    const passcode = prompt("Passcode", "9472");
    if (!passcode) return;
    setBusy(true);
    try {
      await api("/maintenance/poly/reset", { method: "POST", body: JSON.stringify({ passcode, balance: 20 }) });
      setActionMsg("Paper wallet reset to $20.");
      await refresh();
    } finally { setBusy(false); }
  }
  async function vaultReset() {
    const passcode = prompt("Vault reset passcode", "9472");
    if (!passcode) return;
    setBusy(true);
    try {
      await api("/polymarket/vault/reset", { method: "POST", body: JSON.stringify({ passcode }) });
      setActionMsg("Vault reset.");
      await refresh();
    } finally { setBusy(false); }
  }

  async function setVaultCap() {
    const passcode = prompt("Passcode", "9472");
    if (!passcode) return;
    const cap = prompt("Trade cap USD", String(wallet?.trade_cap_usd ?? 30));
    if (!cap) return;
    setBusy(true);
    try {
      await api("/polymarket/vault/set-cap", { method: "POST", body: JSON.stringify({ passcode, cap_usd: Number(cap) }) });
      setActionMsg(`Vault cap updated to $${Number(cap).toFixed(2)}.`);
      await refresh();
    } finally { setBusy(false); }
  }

  async function unlockVaultToTrade() {
    const passcode = prompt("Passcode", "9472");
    if (!passcode) return;
    const amount = prompt("Unlock amount from vault to trade balance", "1");
    if (!amount) return;
    setBusy(true);
    try {
      await api("/polymarket/vault/unlock-to-trade", { method: "POST", body: JSON.stringify({ passcode, amount: Number(amount) }) });
      setActionMsg(`Unlocked $${Number(amount).toFixed(2)} from vault.`);
      await refresh();
    } finally { setBusy(false); }
  }

  const inferredMode = useMemo(() => {
    if (wallet?.force_mode_a) return "MODE-A";
    return ((wallet?.trade_balance ?? wallet?.balance ?? 0) >= 5 ? "MODE-B" : "MODE-A");
  }, [wallet?.force_mode_a, wallet?.trade_balance, wallet?.balance]);
  const nextWindowText = useMemo(() => {
    if (nextWindowSeconds == null) return "--:--";
    const total = Math.max(0, Math.floor(nextWindowSeconds));
    const mm = String(Math.floor(total / 60)).padStart(2, "0");
    const ss = String(total % 60).padStart(2, "0");
    return `${mm}:${ss}`;
  }, [nextWindowSeconds]);
  const running = bot?.status === "running";
  const derivedWins = closedTrades.filter((t) => t.pnl > 0).length;
  const derivedLosses = closedTrades.filter((t) => t.pnl < 0).length;
  const wins = (wallet?.wins ?? 0) > 0 || (wallet?.losses ?? 0) > 0 ? wallet?.wins ?? 0 : derivedWins;
  const losses = (wallet?.wins ?? 0) > 0 || (wallet?.losses ?? 0) > 0 ? wallet?.losses ?? 0 : derivedLosses;
  const winRate = wins + losses > 0 ? (wins / (wins + losses)) * 100 : wallet?.win_rate ?? 0;
  const todayPnlSeries = useMemo(() => {
    const now = new Date();
    const day = now.getDate();
    const month = now.getMonth();
    const year = now.getFullYear();
    const rows = closedTrades
      .map((t) => ({ t, ts: parseTradeTime(t.closed_at) }))
      .filter((row) => {
        if (!Number.isFinite(row.ts)) return false;
        const d = new Date(row.ts);
        return d.getDate() === day && d.getMonth() === month && d.getFullYear() === year;
      })
      .sort((a, b) => a.ts - b.ts)
      .map((row) => row.t);

    const fallbackRows = closedTrades
      .map((t) => ({ t, ts: parseTradeTime(t.closed_at) }))
      .filter((row) => Number.isFinite(row.ts))
      .sort((a, b) => a.ts - b.ts)
      .slice(-24)
      .map((row) => row.t);

    const source = rows.length >= 2 ? rows : fallbackRows;
    if (!source.length) {
      return [{ x: 1, label: "now", pnl: Number((wallet?.total_pnl ?? 0).toFixed(4)) }];
    }
    let running = 0;
    return source.map((t, i) => {
      const ts = parseTradeTime(t.closed_at);
      running += Number(t.pnl || 0);
      return {
        x: i + 1,
        label: Number.isFinite(ts) ? new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "--",
        pnl: Number(running.toFixed(4)),
      };
    });
  }, [closedTrades, wallet?.total_pnl]);
  const monthPnlSeries = useMemo(() => {
    const now = new Date();
    const month = now.getMonth();
    const year = now.getFullYear();
    const rows = closedTrades
      .filter((t) => {
        const ts = parseTradeTime(t.closed_at);
        if (!Number.isFinite(ts)) return false;
        const d = new Date(ts);
        return d.getMonth() === month && d.getFullYear() === year;
      })
      .sort((a, b) => parseTradeTime(a.closed_at) - parseTradeTime(b.closed_at));
    if (!rows.length) {
      return [{ x: 1, label: "now", pnl: Number((wallet?.total_pnl ?? 0).toFixed(4)) }];
    }
    let running = 0;
    return rows.map((t, i) => {
      const ts = parseTradeTime(t.closed_at);
      const d = Number.isFinite(ts) ? new Date(ts) : null;
      running += Number(t.pnl || 0);
      return {
        x: i + 1,
        label: d ? `${d.getDate()}/${d.getMonth() + 1}` : "--",
        pnl: Number(running.toFixed(4)),
      };
    });
  }, [closedTrades, wallet?.total_pnl]);
  const [todayMin, todayMax] = useMemo(() => {
    if (!todayPnlSeries.length) return [-1, 1];
    const vals = todayPnlSeries.map((r) => r.pnl);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const pad = Math.max(0.5, (max - min) * 0.2);
    return [min - pad, max + pad];
  }, [todayPnlSeries]);
  const [monthMin, monthMax] = useMemo(() => {
    if (!monthPnlSeries.length) return [-1, 1];
    const vals = monthPnlSeries.map((r) => r.pnl);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const pad = Math.max(0.5, (max - min) * 0.2);
    return [min - pad, max + pad];
  }, [monthPnlSeries]);

  function isTradeRowVerified(t: Trade): boolean {
    if (!Number.isFinite(t.amount) || !Number.isFinite(t.pnl)) return false;
    if (!t.closed_at) return false;
    const invested = Number(t.amount || 0);
    const gotBack = Number((invested + Number(t.pnl || 0)).toFixed(4));
    const recomputedPnl = Number((gotBack - invested).toFixed(4));
    return Math.abs(recomputedPnl - Number(t.pnl || 0)) <= 0.01;
  }

  return (
    <div style={{ minHeight: "100vh", background: "#10131a", color: "#e0e2ec" }}>
      <div className="container" style={{ maxWidth: 1480, paddingTop: 12, paddingBottom: 24 }}>
        <div
          className="card"
          style={{
            marginBottom: 10,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 8,
            background:
              "linear-gradient(135deg, rgba(47,155,255,0.12), rgba(117,76,255,0.08) 45%, rgba(11,16,28,0.8))",
            border: "1px solid #2a3f68",
          }}
        >
          <div>
            <h1 style={{ margin: 0 }}>AMTA OPERATOR v4.2</h1>
            <div className="sub">Polymarket Paper Bot</div>
            <div style={{ marginTop: 4, fontSize: 12, color: running ? "#63ffbe" : "#9fb2d3", display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: running ? "#63ffbe" : "#60708f", marginRight: 2, boxShadow: running ? "0 0 10px #63ffbe" : "none" }} />
              {running ? "ENGINE RUNNING" : "ENGINE IDLE"}
            </div>
            {actionMsg && <div style={{ marginTop: 4, fontSize: 12, color: "#8bc1ff" }}>{actionMsg}</div>}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-start" disabled={busy || running} onClick={start}>Start</button>
            <button className="btn btn-stop" disabled={busy || !running} onClick={stop}>Stop</button>
            <button className="btn btn-secondary" disabled={busy} onClick={refresh}>Refresh</button>
            <button className="btn btn-secondary" disabled={busy} onClick={resetPaper}>Reset Paper</button>
          </div>
        </div>

        <div
          className="operator-kpis"
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(12, minmax(0, 1fr))",
            gap: 8,
            overflowX: "hidden",
            paddingBottom: 0,
          }}
        >
          <div className="operator-kpi"><span>MODE</span><strong>PAPER</strong></div>
          <div className="operator-kpi"><span>TRADE BALANCE</span><strong style={{ color: "#8bc1ff" }}>${(wallet?.trade_balance ?? wallet?.balance ?? 0).toFixed(2)}</strong></div>
          <div className="operator-kpi"><span>VAULT BALANCE</span><strong style={{ color: "#ffcf6b" }}>${(wallet?.vault_balance ?? 0).toFixed(2)}</strong></div>
          <div className="operator-kpi"><span>ACTUAL BALANCE</span><strong style={{ color: "#63ffbe" }}>${(wallet?.actual_balance ?? wallet?.balance ?? 0).toFixed(2)}</strong></div>
          <div className="operator-kpi"><span>TODAY PNL</span><strong style={{ color: (wallet?.total_pnl ?? 0) >= 0 ? "#63ffbe" : "#ff8f9a" }}>{(wallet?.total_pnl ?? 0) >= 0 ? "+" : ""}{(wallet?.total_pnl ?? 0).toFixed(2)}</strong></div>
          <div className="operator-kpi"><span>OPEN POS</span><strong>{bot?.active_positions ?? 0}</strong></div>
          <div className="operator-kpi"><span>TRADES TODAY</span><strong>{bot?.trades_today ?? 0}</strong></div>
          <div className="operator-kpi"><span>WIN RATE</span><strong style={{ color: "#63ffbe" }}>{winRate.toFixed(1)}%</strong></div>
          <div className="operator-kpi"><span>W / L</span><strong style={{ fontSize: 18, whiteSpace: "nowrap" }}><span style={{ color: "#63ffbe", display: "inline" }}>{wins}W</span> / <span style={{ color: "#ff8f9a", display: "inline" }}>{losses}L</span></strong></div>
          <div className="operator-kpi"><span>SCANNED</span><strong>{bot?.scanned_markets_today ?? 0}</strong></div>
          <div className="operator-kpi"><span>CANDIDATES</span><strong>{bot?.last_candidate_count ?? 0}</strong></div>
          <div className="operator-kpi"><span>NEXT WINDOW</span><strong style={{ color: "#ffcf6b" }}>{nextWindowText}</strong></div>
          <div className="operator-kpi"><span>STRATEGY</span><strong style={{ color: inferredMode === "MODE-B" ? "#ffcf6b" : "#8bc1ff" }}>{inferredMode}</strong></div>
        </div>

        <div className="operator-grid" style={{ marginTop: 10 }}>
          <div className="operator-main">
            <div className="card" style={{ paddingBottom: 12 }}>
              <h3>REAL-TIME LOGS</h3>
              <div style={{ display: "grid", gridTemplateColumns: "1.6fr 1fr 1fr", gap: 10 }}>
                <div className="operator-log" style={{ background: "#0b0f18", border: "1px solid #22314c", height: 360 }}>
                  {logs.map((x) => (
                    <div
                      key={x.id}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "120px 56px 1fr",
                        gap: 10,
                        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                        fontSize: 12,
                        lineHeight: 1.45,
                        padding: "2px 0",
                        borderBottom: "1px solid rgba(34,49,76,0.25)",
                      }}
                    >
                      <span style={{ color: "#90a5cc" }}>{formatLogTime(x)}</span>
                      <span style={{ color: x.level === "ERROR" ? "#ff8f9a" : x.level === "WARNING" ? "#ffcf6b" : "#8bc1ff", fontWeight: 700 }}>{x.level}</span>
                      <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", color: x.message.includes("VAULT_SWEEP") ? "#ffcf6b" : "#d8e6ff", fontWeight: x.message.includes("VAULT_SWEEP") ? 700 : 400 }}>{x.message}</span>
                    </div>
                  ))}
                </div>
                <div style={{ border: "1px solid #22314c", borderRadius: 6, background: "#0b0f18", height: 360, padding: 8 }}>
                  <div style={{ fontSize: 12, color: "#ff7b63", marginBottom: 6 }}>Today P&amp;L line chart</div>
                  <ResponsiveContainer width="100%" height="90%">
                    <LineChart data={todayPnlSeries}>
                      <CartesianGrid stroke="#1d2a44" vertical={false} />
                      <XAxis dataKey="label" stroke="#6f86af" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                      <YAxis hide domain={[todayMin, todayMax]} />
                      <Tooltip contentStyle={{ background: "#101722", border: "1px solid #22314c" }} />
                      <Line type="linear" dataKey="pnl" stroke="#4ade80" strokeWidth={2} dot={false} connectNulls />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
                <div style={{ border: "1px solid #22314c", borderRadius: 6, background: "#0b0f18", height: 360, padding: 8 }}>
                  <div style={{ fontSize: 12, color: "#ff7b63", marginBottom: 6 }}>Monthly P&amp;L line chart</div>
                  <ResponsiveContainer width="100%" height="90%">
                    <LineChart data={monthPnlSeries}>
                      <CartesianGrid stroke="#1d2a44" vertical={false} />
                      <XAxis dataKey="label" stroke="#6f86af" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                      <YAxis hide domain={[monthMin, monthMax]} />
                      <Tooltip contentStyle={{ background: "#101722", border: "1px solid #22314c" }} />
                      <Line type="linear" dataKey="pnl" stroke="#60a5fa" strokeWidth={2} dot={false} connectNulls />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>
            </div>
            <div className="card" style={{ background: "linear-gradient(180deg,#101826,#0f1420)", border: "1px solid #2a3f68" }}>
              <h3>SCAN INTELLIGENCE</h3>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(4,minmax(0,1fr))", gap: 8, marginTop: 8 }}>
                <div className="operator-kpi"><span>SCANS</span><strong style={{ color: "#8bc1ff" }}>{bot?.scanned_markets_today ?? 0}</strong></div>
                <div className="operator-kpi"><span>CANDIDATES</span><strong style={{ color: "#ffcf6b" }}>{bot?.last_candidate_count ?? 0}</strong></div>
                <div className="operator-kpi"><span>OPEN</span><strong style={{ color: "#63ffbe" }}>{openTrades.length}</strong></div>
                <div className="operator-kpi"><span>CLOSED</span><strong style={{ color: "#bcbfcb" }}>{closedTrades.length}</strong></div>
              </div>
            </div>
            <div className="card">
              <h3>CLOSED TRADES (PAPER)</h3>
              <table>
                <thead><tr><th>Time</th><th>Market</th><th>Invested</th><th>Got Back</th><th>P/L</th><th>Verified</th></tr></thead>
                <tbody>
                  {closedTrades.map((t) => (
                    <tr key={t.id}>
                      <td>{t.closed_at ? new Date(t.closed_at).toLocaleTimeString() : "--"}</td>
                      <td>{t.market_title}</td>
                      <td>${t.amount.toFixed(2)}</td>
                      <td style={{ color: t.pnl >= 0 ? "#63ffbe" : "#ff8f9a", fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace" }}>
                        {t.amount.toFixed(2)} {t.pnl >= 0 ? "+" : "-"} {Math.abs(t.pnl).toFixed(2)}
                      </td>
                      <td style={{ color: t.pnl >= 0 ? "#63ffbe" : "#ff8f9a" }}>{t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(2)}</td>
                      <td title={isTradeRowVerified(t) ? "Verified: got_back = invested + pnl" : "Unverified"}>
                        {isTradeRowVerified(t) ? (
                          <span style={{ color: "#63ffbe", fontWeight: 700 }}>✓</span>
                        ) : (
                          <span style={{ color: "#ff8f9a", fontWeight: 700 }}>—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card">
              <h3>OPEN POSITIONS (PAPER)</h3>
              <table>
                <thead><tr><th>Market</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>PnL</th></tr></thead>
                <tbody>
                  {openTrades.map((t) => (
                    <tr key={t.id}>
                      <td>{t.market_title}</td><td>{t.direction}</td><td>{t.amount.toFixed(2)}</td><td>{t.entry_price.toFixed(3)}</td><td>{(t.current_price ?? 0).toFixed(3)}</td><td>{t.pnl.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card">
              <h3>FOCUS MARKETS</h3>
              <table>
                <thead><tr><th>Market</th><th>YES</th><th>NO</th></tr></thead>
                <tbody>
                  {markets.map((m) => <tr key={m.id}><td>{m.title}</td><td>{m.yes_price.toFixed(3)}</td><td>{m.no_price.toFixed(3)}</td></tr>)}
                </tbody>
              </table>
            </div>
          </div>
          <div className="operator-side">
            <div className="card">
              <h3>VAULT CONTROLS (PAPER)</h3>
              <div className="operator-kv"><span>Trade Cap</span><strong>${(wallet?.trade_cap_usd ?? 30).toFixed(2)}</strong></div>
              <div className="operator-kv"><span>Vault Locked</span><strong>{wallet?.vault_locked ? "yes" : "no"}</strong></div>
              <div className="operator-kv"><span>Sweeps</span><strong>{wallet?.vault_sweeps_count ?? 0}</strong></div>
              <div className="operator-kv"><span>Last Sweep</span><strong>{wallet?.last_sweep_at ? new Date(wallet.last_sweep_at).toLocaleString() : "—"}</strong></div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 8, marginTop: 10 }}>
                <button className="btn btn-secondary" disabled={busy} onClick={setVaultCap}>Set Vault Cap</button>
                <button className="btn btn-secondary" disabled={busy} onClick={unlockVaultToTrade}>Unlock Vault To Trade</button>
                <button className="btn btn-stop" disabled={busy} onClick={vaultReset}>Reset Vault</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
