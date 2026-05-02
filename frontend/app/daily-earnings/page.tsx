"use client";

import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../../lib/api";

type Trade = {
  id: string;
  pnl: number;
  status: string;
  closed_at?: string | null;
  opened_at: string;
};

type DayRow = {
  day: string;
  pnl: number;
  trades: number;
  wins: number;
  losses: number;
  strike: number;
  cumulative: number;
};

function toDayKey(iso: string): string {
  const d = new Date(iso);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export default function DailyEarningsPage() {
  const [rows, setRows] = useState<DayRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setBusy(true);
    try {
      const closed = await api<Trade[]>("/trades?status=closed&limit=1000&page=1");
      const map = new Map<string, Omit<DayRow, "day" | "cumulative" | "strike">>();
      for (const t of closed) {
        const when = t.closed_at || t.opened_at;
        const key = toDayKey(when);
        if (!map.has(key)) map.set(key, { pnl: 0, trades: 0, wins: 0, losses: 0 });
        const r = map.get(key)!;
        r.pnl += t.pnl || 0;
        r.trades += 1;
        if ((t.pnl || 0) > 0) r.wins += 1;
        if ((t.pnl || 0) < 0) r.losses += 1;
      }
      const sorted = [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
      let acc = 0;
      const out: DayRow[] = sorted.map(([day, r]) => {
        acc += r.pnl;
        return {
          day,
          pnl: Number(r.pnl.toFixed(4)),
          trades: r.trades,
          wins: r.wins,
          losses: r.losses,
          strike: r.trades ? Number(((r.wins / r.trades) * 100).toFixed(2)) : 0,
          cumulative: Number(acc.toFixed(4)),
        };
      });
      setRows(out.reverse()); // latest first in table
      setError(null);
    } catch (e: any) {
      setError(e?.message || "failed to load daily earnings");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const chartData = useMemo(() => [...rows].reverse(), [rows]); // oldest -> newest
  const totalPnl = useMemo(() => rows.reduce((a, r) => a + r.pnl, 0), [rows]);

  return (
    <div className="container">
      <h1>Daily Earnings</h1>
      <div className="sub">Day-wise P&L from closed trades.</div>

      <div className="row" style={{ marginBottom: 10 }}>
        <div className="card">
          <h3>Net Daily P&L</h3>
          <div className={totalPnl >= 0 ? "big pos" : "big neg"}>
            {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(4)}
          </div>
          <div className="sub" style={{ margin: 0 }}>
            {rows.length} trading day{rows.length === 1 ? "" : "s"}
          </div>
        </div>
        <div className="card" style={{ display: "flex", alignItems: "center", justifyContent: "flex-end" }}>
          <button className="btn btn-secondary" onClick={() => void refresh()} disabled={busy}>
            {busy ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      {error && (
        <div className="card" style={{ marginBottom: 10 }}>
          <div className="sub" style={{ color: "#ff7b7b", margin: 0 }}>{error}</div>
        </div>
      )}

      <div className="dashx-grid-mid">
        <div className="card dashx-chart-card">
          <h3>Daily P&L</h3>
          <div className="dashx-chart-wrap">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData}>
                <CartesianGrid stroke="#1c2436" vertical={false} />
                <XAxis dataKey="day" stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                <YAxis stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                <Tooltip contentStyle={{ background: "#101722", border: "1px solid #22314c" }} />
                <Bar dataKey="pnl" fill="#2f9bff" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="card dashx-chart-card">
          <h3>Cumulative Growth</h3>
          <div className="dashx-chart-wrap">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData}>
                <CartesianGrid stroke="#1c2436" vertical={false} />
                <XAxis dataKey="day" stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                <YAxis stroke="#6f7f9d" tick={{ fontSize: 11 }} />
                <Tooltip contentStyle={{ background: "#101722", border: "1px solid #22314c" }} />
                <Line type="linear" dataKey="cumulative" stroke="#22c55e" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Day</th>
              <th>P&L</th>
              <th>Trades</th>
              <th>Wins</th>
              <th>Losses</th>
              <th>Strike Rate</th>
              <th>Cumulative</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="sub">No closed trades yet.</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.day}>
                <td>{r.day}</td>
                <td className={r.pnl >= 0 ? "pos" : "neg"}>{r.pnl >= 0 ? "+" : ""}{r.pnl.toFixed(4)}</td>
                <td>{r.trades}</td>
                <td>{r.wins}</td>
                <td>{r.losses}</td>
                <td>{r.strike.toFixed(2)}%</td>
                <td className={r.cumulative >= 0 ? "pos" : "neg"}>
                  {r.cumulative >= 0 ? "+" : ""}{r.cumulative.toFixed(4)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

