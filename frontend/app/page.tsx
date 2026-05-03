"use client";
import { useEffect, useRef, useState } from "react";
import { api, WS } from "../lib/api";
import { LineChart, Line, XAxis, YAxis, ResponsiveContainer, Tooltip, BarChart, Bar, CartesianGrid } from "recharts";

type Wallet = {
  balance: number; total_pnl: number; total_trades: number;
  wins: number; losses: number; win_rate: number;
};
type BotStatus = {
  status: string; state: string; uptime_seconds: number; trades_today: number;
  active_positions?: number;
  max_concurrent_positions?: number;
};
type TradeSummary = {
  total_count: number;
  open_count: number;
  closed_count: number;
  today_opened_count: number;
  today_closed_count: number;
};
type Trade = {
  id: string; market_id: string; market_title: string; category: string;
  direction: "YES" | "NO"; amount: number; entry_price: number;
  exit_price: number | null; pnl: number; status: string; agent_score: number;
  current_price?: number | null; unrealized_pnl?: number; opened_at: string;
  closed_at?: string | null;
};
type LogEntry = { id?: string; level: string; message: string; created_at?: string; ts?: string };
type RangeLabel = "This Week" | "This Month" | "This Year" | "All Time";

function fmtCurrency(n: number) {
  return `${n >= 0 ? "+" : "-"}$${Math.abs(n).toFixed(2)}`;
}

function inDays(d: Date, n: number) {
  const now = Date.now();
  return now - d.getTime() <= n * 24 * 60 * 60 * 1000;
}

function statForRange(label: RangeLabel, closed: Trade[]) {
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
  const rr = filtered.reduce((acc, t) => acc + (t.pnl || 0), 0);
  return { label, pnl, wins, losses, total, strike, rr };
}

export default function Dashboard() {
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [open, setOpen] = useState<Trade[]>([]);
  const [closed, setClosed] = useState<Trade[]>([]);
  const [allTrades, setAllTrades] = useState<Trade[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [summary, setSummary] = useState<TradeSummary | null>(null);
  const [pnlSeries, setPnl] = useState<{ t: string; pnl: number }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  async function refresh() {
    const safe = async <T,>(p: Promise<T>) => {
      try {
        return { ok: true as const, value: await p };
      } catch (e: any) {
        return { ok: false as const, error: e?.message || "request failed" };
      }
    };
    try {
      const [w, b, o, c, l, s, all] = await Promise.all([
        safe(api<Wallet>("/wallet")),
        safe(api<BotStatus>("/bot/status")),
        safe(api<Trade[]>("/trades?status=open&limit=50")),
        safe(api<Trade[]>("/trades?status=closed&limit=50")),
        safe(api<LogEntry[]>("/agent/logs?limit=50")),
        safe(api<TradeSummary>("/trades/summary")),
        safe(api<Trade[]>("/trades?status=all&limit=500&page=1")),
      ]);

      if (w.ok) setWallet(w.value);
      if (b.ok) setBot(b.value);
      if (o.ok) setOpen(o.value);
      if (c.ok) setClosed(c.value);
      if (all.ok) setAllTrades(all.value);
      if (l.ok) setLogs(l.value);
      if (s.ok) setSummary(s.value);

      // build cumulative P&L vs time from closed trades (oldest -> newest)
      const closedRows = c.ok ? c.value : closed;
      const sorted = [...closedRows].reverse();
      let acc = 0;
      setPnl(sorted.map(t => {
        acc += t.pnl || 0;
        const ts = t.closed_at ? new Date(t.closed_at) : new Date(t.opened_at);
        const label = ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        return { t: label, pnl: Number(acc.toFixed(4)) };
      }));

      const failures = [w, b, o, c, l, s, all].filter((x) => !x.ok);
      if (failures.length === 0) {
        setError(null);
      } else {
        setError(`partial refresh: ${failures.length} endpoint(s) failed`);
      }
    } catch (e: any) {
      setError(e?.message || "refresh failed");
    }
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => { void refresh(); }, 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const ws = new WebSocket(WS);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.event === "agent:log") {
          setLogs((cur) => [{ ...m.data }, ...cur].slice(0, 100));
        }
        if (["trade:opened", "trade:closed", "wallet:updated", "bot:status"].includes(m.event)) {
          refresh();
        }
      } catch {}
    };
    return () => ws.close();
  }, []);

  async function startBot() { setBusy(true); try { await api("/bot/start", { method: "POST" }); await refresh(); } finally { setBusy(false); } }
  async function stopBot() { setBusy(true); try { await api("/bot/stop", { method: "POST" }); await refresh(); } finally { setBusy(false); } }
  async function reloadKb() { setBusy(true); try { const r = await api("/agent/knowledge/reload", { method: "POST" }); alert("Knowledge reload: " + JSON.stringify(r)); } finally { setBusy(false); } }
  async function deposit() {
    const v = prompt("Deposit amount (USD):", "500");
    if (!v) return;
    setBusy(true);
    try { await api("/wallet/deposit", { method: "POST", body: JSON.stringify({ amount: Number(v) }) }); await refresh(); } finally { setBusy(false); }
  }

  const running = bot?.status === "running";
  const pnl = wallet?.total_pnl ?? 0;
  const ranges: RangeLabel[] = ["This Week", "This Month", "This Year", "All Time"];
  const stats = ranges.map((r) => statForRange(r, closed));

  const balanceSeries = (() => {
    if (!wallet) return [];
    type Pt = { ts: number; label: string; delta: number };
    const pts: Pt[] = [];
    for (const t of allTrades) {
      const opened = new Date(t.opened_at).getTime();
      if (!Number.isNaN(opened)) {
        pts.push({
          ts: opened,
          label: `${new Date(opened).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`,
          delta: -Math.abs(t.amount || 0),
        });
      }
      if (t.closed_at) {
        const closedTs = new Date(t.closed_at).getTime();
        if (!Number.isNaN(closedTs)) {
          const payout = (t.amount || 0) + (t.pnl || 0);
          pts.push({
            ts: closedTs,
            label: `${new Date(closedTs).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`,
            delta: payout,
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
    for (let i = 0; i < pts.length; i++) {
      bal += pts[i].delta;
      out.push({
        t: pts[i].label,
        balance: Number(bal.toFixed(2)),
      });
    }
    return out.slice(-250);
  })();

  const rrBuckets = (() => {
    const map = new Map<string, number>();
    for (const t of closed) {
      const d = new Date(t.closed_at || t.opened_at);
      const k = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
      map.set(k, (map.get(k) || 0) + (t.pnl || 0));
    }
    return [...map.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .slice(-12)
      .map(([k, v]) => ({ k: k.slice(2), rr: Number(v.toFixed(2)) }));
  })();

  const rightList = [...closed].slice(0, 12);

  const frequentMarkets = (() => {
    const map = new Map<string, { market: string; n: number; wins: number; pnl: number }>();
    for (const t of allTrades) {
      const key = t.market_id || t.market_title;
      if (!map.has(key)) {
        map.set(key, { market: t.market_title || t.market_id, n: 0, wins: 0, pnl: 0 });
      }
      const row = map.get(key)!;
      row.n += 1;
      if ((t.pnl || 0) > 0) row.wins += 1;
      row.pnl += t.pnl || 0;
    }
    return [...map.values()]
      .sort((a, b) => b.n - a.n || b.pnl - a.pnl)
      .slice(0, 10)
      .map((r) => ({
        ...r,
        winRate: r.n ? (r.wins / r.n) * 100 : 0,
      }));
  })();

  const tradeFreq = (() => {
    const map = new Map<string, number>();
    for (const t of allTrades) {
      const dt = new Date(t.opened_at);
      const label = `${String(dt.getHours()).padStart(2, "0")}:00`;
      map.set(label, (map.get(label) || 0) + 1);
    }
    return [...map.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([hour, trades]) => ({ hour, trades }));
  })();

  const monthStats = (() => {
    const years = new Map<number, Record<number, { pnl: number; n: number; wins: number }>>();
    for (const t of closed) {
      const d = new Date(t.closed_at || t.opened_at);
      const y = d.getFullYear();
      const m = d.getMonth();
      if (!years.has(y)) years.set(y, {});
      const row = years.get(y)!;
      if (!row[m]) row[m] = { pnl: 0, n: 0, wins: 0 };
      row[m].pnl += t.pnl || 0;
      row[m].n += 1;
      if ((t.pnl || 0) > 0) row[m].wins += 1;
    }
    return [...years.entries()].sort((a, b) => b[0] - a[0]);
  })();

  return (
    <div className="container dashx">
      <div className="dashx-top">
        <div>
          <h1>🤖 AMTA Dashboard</h1>
          <div className="sub">Paper trading analytics · research-driven execution</div>
        </div>
        <div className="dashx-actions">
          {!running ? (
            <button className="btn btn-start" onClick={startBot} disabled={busy}>▶ Start Bot</button>
          ) : (
            <button className="btn btn-stop" onClick={stopBot} disabled={busy}>■ Stop Bot</button>
          )}
          <button className="btn btn-secondary" onClick={reloadKb} disabled={busy}>Reload PDFs</button>
          <button className="btn btn-secondary" onClick={deposit} disabled={busy}>Deposit</button>
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
            <div className="dashx-kpi-sub">{s.rr.toFixed(2)} R:R</div>
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
            <strong>{summary?.today_opened_count ?? bot?.trades_today ?? 0} opened</strong>
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
            <span className="sub">Open: {summary?.open_count ?? open.length}</span>
          </div>
          <div className="dashx-side-list">
            {rightList.length === 0 && <div className="sub">No recent closed trades.</div>}
            {rightList.map((t) => (
              <div className="dashx-side-item" key={t.id}>
                <div className="dashx-side-title">{t.market_title.slice(0, 40)}</div>
                <div className="dashx-side-sub">{t.category} · {t.direction} · {t.status.replace("CLOSED_", "")}</div>
                <div className={t.pnl >= 0 ? "pos" : "neg"}>{t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(4)}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <h2>Monthly Stats</h2>
      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Year</th>
              <th>Jan</th><th>Feb</th><th>Mar</th><th>Apr</th><th>May</th><th>Jun</th>
              <th>Jul</th><th>Aug</th><th>Sep</th><th>Oct</th><th>Nov</th><th>Dec</th>
              <th>Total</th>
            </tr>
          </thead>
          <tbody>
            {monthStats.length === 0 && <tr><td colSpan={14} className="sub">No closed trades yet.</td></tr>}
            {monthStats.map(([year, row]) => {
              const vals = Array.from({ length: 12 }, (_, m) => row[m]?.pnl ?? 0);
              const total = vals.reduce((a, b) => a + b, 0);
              return (
                <tr key={year}>
                  <td>{year}</td>
                  {vals.map((v, i) => (
                    <td key={i} className={v >= 0 ? "pos" : "neg"}>{v === 0 ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}`}</td>
                  ))}
                  <td className={total >= 0 ? "pos" : "neg"}>{total >= 0 ? "+" : ""}{total.toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <h2>Frequent Markets</h2>
      <div className="dashx-grid-mid">
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Market</th>
                <th>Trades</th>
                <th>Wins</th>
                <th>Win Rate</th>
                <th>Total P&L</th>
              </tr>
            </thead>
            <tbody>
              {frequentMarkets.length === 0 && (
                <tr><td colSpan={5} className="sub">No trade history yet.</td></tr>
              )}
              {frequentMarkets.map((m, i) => (
                <tr key={`${m.market}-${i}`}>
                  <td title={m.market}>{m.market.slice(0, 70)}</td>
                  <td>{m.n}</td>
                  <td>{m.wins}</td>
                  <td>{m.winRate.toFixed(1)}%</td>
                  <td className={m.pnl >= 0 ? "pos" : "neg"}>
                    {m.pnl >= 0 ? "+" : ""}{m.pnl.toFixed(4)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card dashx-chart-card">
          <h3>Trade Frequency (Hourly)</h3>
          <div className="dashx-chart-wrap">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={tradeFreq}>
                <CartesianGrid stroke="#1c2436" vertical={false} />
                <XAxis dataKey="hour" stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                <YAxis stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                <Tooltip contentStyle={{ background: "#101722", border: "1px solid #22314c" }} />
                <Bar dataKey="trades" fill="#2f9bff" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <h2>Open Positions</h2>
      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead><tr><th>Market</th><th>Cat</th><th>Side</th><th>Score</th><th>Entry</th><th>Now</th><th>Unreal P&L</th></tr></thead>
          <tbody>
            {open.length === 0 && <tr><td colSpan={7} className="sub">No open positions.</td></tr>}
            {open.slice(0, 20).map(t => (
              <tr key={t.id}>
                <td title={t.market_title}>{t.market_title.slice(0, 50)}</td>
                <td className="sub">{t.category}</td>
                <td><span className={`badge badge-${t.direction === "YES" ? "yes" : "no"}`}>{t.direction}</span></td>
                <td>{t.agent_score}</td>
                <td>{t.entry_price.toFixed(2)}</td>
                <td>{t.current_price != null ? t.current_price.toFixed(2) : "—"}</td>
                <td className={(t.unrealized_pnl ?? 0) >= 0 ? "pos" : "neg"}>
                  {t.unrealized_pnl != null ? (t.unrealized_pnl >= 0 ? "+" : "") + t.unrealized_pnl.toFixed(4) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Agent Activity</h2>
      <div className="card feed">
        {logs.length === 0 && <div className="sub">No logs yet — start the bot.</div>}
        {logs.map((l, i) => (
          <div key={i} className="row">
            <span className={`lvl-${l.level}`}>[{l.level}]</span>
            <span>{l.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
