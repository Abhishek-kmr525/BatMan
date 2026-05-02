"use client";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";

type Strategy = {
  id: string; name: string; description: string; min_score: number;
  is_active: boolean;
  trades_total: number; trades_open: number; trades_closed: number;
  wins: number; win_rate: number; total_pnl: number;
};

type PreviewMarket = {
  ticker: string; title: string; yes_price: number;
  volume: number; close_time_seconds: number;
};

export default function StrategiesPage() {
  const [strats, setStrats] = useState<Strategy[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewMarket[] | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    const r = await api<{ strategies: Strategy[]; active_ids: string[] }>("/strategies");
    setStrats(r.strategies);
    setActiveId(r.active_ids[0] ?? null);
  }
  useEffect(() => { load(); }, []);

  async function toggle(id: string, active: boolean) {
    setBusy(true);
    try {
      await api("/strategies/activate", {
        method: "POST",
        body: JSON.stringify({ strategy_id: id, active }),
      });
      await load();
    } finally { setBusy(false); }
  }
  async function deactivateAll() {
    setBusy(true);
    try {
      await api("/strategies/deactivate_all", { method: "POST" });
      await load();
    } finally { setBusy(false); }
  }

  async function showPreview(id: string) {
    setPreviewing(id);
    setPreview(null);
    const r = await api<{ markets: PreviewMarket[] }>(`/strategies/${id}/preview`);
    setPreview(r.markets);
  }

  return (
    <div className="container">
      <h1>Strategy Marketplace</h1>
      <div className="sub">
        Activate one or more strategies — the bot will only consider markets matching at least one active filter.
        When several strategies match the same market, the first one activated claims it (and tags the trade).
        Closed positions stay tagged with the strategy that opened them.
      </div>
      {strats.some(s => s.is_active) && (
        <div style={{ marginTop: 10 }}>
          <button className="btn btn-secondary" onClick={deactivateAll} disabled={busy}>Deactivate all</button>
        </div>
      )}

      <div className="row" style={{ marginTop: 16 }}>
        {strats.map(s => (
          <div key={s.id} className="card" style={{ flexBasis: 360, position: "relative" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
              <div>
                <div style={{ fontWeight: 600, fontSize: 16 }}>{s.name}</div>
                <div className="sub" style={{ marginTop: 4 }}>min score {s.min_score}</div>
              </div>
              {s.is_active && <span className="badge badge-running">ACTIVE</span>}
            </div>
            <div className="sub" style={{ marginTop: 10, color: "#c0c5d0", fontSize: 13 }}>{s.description}</div>
            <div className="sub" style={{ marginTop: 12, display: "flex", gap: 12 }}>
              <span>trades: <b>{s.trades_total}</b></span>
              <span>open: <b>{s.trades_open}</b></span>
              <span>win rate: <b>{s.win_rate}%</b></span>
              <span className={s.total_pnl >= 0 ? "pos" : "neg"}>P&L {s.total_pnl >= 0 ? "+" : ""}{s.total_pnl.toFixed(4)}</span>
            </div>
            <div style={{ marginTop: 14, display: "flex", gap: 8 }}>
              {!s.is_active ? (
                <button className="btn btn-start" onClick={() => toggle(s.id, true)} disabled={busy}>Activate</button>
              ) : (
                <button className="btn btn-stop" onClick={() => toggle(s.id, false)} disabled={busy}>Deactivate</button>
              )}
              <button className="btn btn-secondary" onClick={() => showPreview(s.id)} disabled={busy}>Preview matches</button>
            </div>
          </div>
        ))}
      </div>

      {previewing && (
        <div style={{ marginTop: 28 }}>
          <h2>Preview: {strats.find(s => s.id === previewing)?.name}</h2>
          <div className="card" style={{ overflowX: "auto" }}>
            {preview === null ? (
              <div className="sub">Loading…</div>
            ) : preview.length === 0 ? (
              <div className="sub">No live markets currently match this strategy.</div>
            ) : (
              <table>
                <thead><tr><th>Market</th><th>YES</th><th>Vol</th><th>Closes in</th></tr></thead>
                <tbody>
                  {preview.map(m => (
                    <tr key={m.ticker}>
                      <td title={m.ticker}>{m.title.slice(0, 70)}</td>
                      <td>${m.yes_price.toFixed(2)}</td>
                      <td>{m.volume}</td>
                      <td className="sub">{Math.floor(m.close_time_seconds / 60)}m</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
