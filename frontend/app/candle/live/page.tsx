"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "../../../lib/api";
import CandleChart, { Candle, ChartLevel, Marker } from "../../../components/CandleChart";

type ModeState = { mode: "paper" | "live_requested" | "live_armed"; live_enabled: boolean; kill_switch: boolean };
type ModeSnapshot = { candle: ModeState };

type BotStatus = {
  bot_kind: "paper" | "live";
  status: "running" | "stopped";
  state: string;
  uptime_seconds: number;
  trades_today: number;
  last_scan_count: number;
  last_signal_count: number;
  consecutive_losses: number;
  daily_loss_usd: number;
  in_cooldown: boolean;
  cooldown_until_epoch: number | null;
  started_at: string | null;
  active_positions: number;
  strategy_id?: string;
  mode_guard?: ModeState;
};

type Wallet = {
  mode: "paper" | "live";
  balance: number;
  paper_starting_balance: number;
  live_balance_updated_at: string | null;
  live_error: string | null;
  total_pnl: number;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  symbols: string[];
  primary_interval: string;
  htf_interval: string;
  risk_per_trade_pct: number;
  rr_ratio: number;
};

type Trade = {
  id: string;
  symbol: string;
  interval: string;
  direction: "LONG" | "SHORT";
  qty: number;
  notional_usd: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  current_price: number | null;
  exit_price: number | null;
  pnl_usd: number;
  pnl_pct: number;
  status: string;
  htf_bias: string;
  setup_type: string;
  confidence: number;
  reasoning: string;
  opened_at: string | null;
  closed_at: string | null;
};

type LogEntry = { id: string; level: string; message: string; ts: string | null };

type Signal = {
  symbol: string;
  strategy_id?: string;
  direction: "LONG" | "SHORT" | "SKIP";
  confidence: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  rr_ratio: number;
  htf_bias: string;
  setup_type: string;
  reasoning: string;
};
type StrategyItem = { id: string; label: string };
type StrategyResponse = { default: string; items: StrategyItem[] };

const INTERVALS = ["1m", "5m", "15m", "1h", "4h"] as const;

export default function CandleLivePage() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [interval, setInterval] = useState<string>("5m");
  const [mode, setMode] = useState<ModeState | null>(null);
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [closedTrades, setClosedTrades] = useState<Trade[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [signal, setSignal] = useState<Signal | null>(null);
  const [strategies, setStrategies] = useState<StrategyItem[]>([]);
  const [strategyId, setStrategyId] = useState("sweep_bos_v1");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function refresh() {
    try {
      const [m, b, w, k, ot, ct, lg, sg, st] = await Promise.all([
        api<ModeSnapshot>("/mode/status"),
        api<BotStatus>("/candle/live/bot/status"),
        api<Wallet>("/candle/live/wallet"),
        api<{ symbol: string; interval: string; candles: Candle[] }>(`/candle/klines?symbol=${symbol}&interval=${interval}&limit=200`),
        api<Trade[]>("/candle/trades?mode=live&status=open&limit=20"),
        api<Trade[]>("/candle/trades?mode=live&status=closed&limit=100"),
        api<LogEntry[]>("/candle/logs?mode=live&limit=200"),
        api<Signal>(`/candle/analyze?symbol=${symbol}&strategy_id=${strategyId}`),
        api<StrategyResponse>("/candle/strategies"),
      ]);
      setMode(m.candle);
      setBot(b);
      setWallet(w);
      setCandles(k.candles || []);
      setOpenTrades(ot || []);
      setClosedTrades(ct || []);
      setLogs(lg || []);
      setSignal(sg);
      setStrategies(st.items || []);
      if (st.items?.length && !st.items.find((x) => x.id === strategyId)) {
        setStrategyId(st.default || st.items[0].id);
      }
    } catch (e) {
      console.error("refresh error", e);
    }
  }

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, interval, strategyId]);

  async function armLive() {
    const passcode = window.prompt("Live confirmation passcode:");
    if (!passcode) return;
    setBusy(true);
    try {
      const req = await api<any>("/mode/request-live", { method: "POST", body: JSON.stringify({ platform: "candle" }) });
      if (!req?.ok) {
        setMsg(`Arm request failed: ${req?.error ?? "unknown"}`);
        return;
      }
      const cfm = await api<any>("/mode/confirm-live", { method: "POST", body: JSON.stringify({ platform: "candle", passcode }) });
      if (!cfm?.ok) {
        setMsg(`Confirm failed: ${cfm?.error ?? "unknown"}`);
        return;
      }
      setMsg("Live mode armed.");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function setPaper() {
    setBusy(true);
    try {
      await api("/mode/set-paper", { method: "POST", body: JSON.stringify({ platform: "candle" }) });
      setMsg("Reverted to paper.");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function killToggle() {
    if (!mode) return;
    setBusy(true);
    try {
      await api("/mode/kill-switch", {
        method: "POST",
        body: JSON.stringify({ platform: "candle", enabled: !mode.kill_switch }),
      });
      setMsg(!mode.kill_switch ? "Kill switch enabled." : "Kill switch disabled.");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    setBusy(true);
    try {
      await api("/candle/live/bot/start", {
        method: "POST",
        body: JSON.stringify({ strategy_id: strategyId }),
      });
      setMsg(`Live bot started with ${strategyId}.`);
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  async function stop() {
    setBusy(true);
    try {
      await api("/candle/live/bot/stop", { method: "POST" });
      setMsg("Live bot stopped.");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  const running = bot?.status === "running";
  const balance = wallet?.balance ?? 0;
  const equity = balance + openTrades.reduce((acc, t) => acc + (t.notional_usd ?? 0), 0);
  const totalPnl = wallet?.total_pnl ?? 0;
  const winRate = wallet?.win_rate ?? 0;
  const isArmed = mode?.mode === "live_armed";

  const levels: ChartLevel[] = useMemo(() => {
    const out: ChartLevel[] = [];
    for (const t of openTrades) {
      if (t.symbol !== symbol) continue;
      out.push({ price: t.entry_price, color: "#8bc1ff", label: `ENTRY ${t.entry_price.toFixed(2)}` });
      out.push({ price: t.stop_loss, color: "#ff8f9a", label: `SL ${t.stop_loss.toFixed(2)}` });
      out.push({ price: t.take_profit, color: "#63ffbe", label: `TP ${t.take_profit.toFixed(2)}` });
    }
    return out;
  }, [openTrades, symbol]);

  const markers: Marker[] = useMemo(() => {
    const out: Marker[] = [];
    const sample = [...openTrades, ...closedTrades].filter((t) => t.symbol === symbol);
    for (const t of sample) {
      if (t.opened_at) {
        out.push({
          time: Math.floor(new Date(t.opened_at).getTime() / 1000),
          position: t.direction === "LONG" ? "belowBar" : "aboveBar",
          color: t.direction === "LONG" ? "#63ffbe" : "#ff8f9a",
          shape: t.direction === "LONG" ? "arrowUp" : "arrowDown",
          text: t.direction,
        });
      }
      if (t.closed_at) {
        out.push({
          time: Math.floor(new Date(t.closed_at).getTime() / 1000),
          position: t.pnl_usd >= 0 ? "aboveBar" : "belowBar",
          color: t.pnl_usd >= 0 ? "#63ffbe" : "#ff8f9a",
          shape: "circle",
          text: t.pnl_usd >= 0 ? `+${t.pnl_usd.toFixed(2)}` : t.pnl_usd.toFixed(2),
        });
      }
    }
    out.sort((a, b) => a.time - b.time);
    return out;
  }, [openTrades, closedTrades, symbol]);

  return (
    <div style={{ minHeight: "100vh", background: "#10131a", color: "#e0e2ec" }}>
      <div className="container" style={{ maxWidth: 1480, paddingTop: 12, paddingBottom: 24 }}>
        {/* Header */}
        <div
          className="card"
          style={{
            marginBottom: 10,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 8,
            background:
              "linear-gradient(135deg, rgba(255,143,154,0.10), rgba(255,207,107,0.06) 45%, rgba(11,16,28,0.85))",
            border: "1px solid #5a2a3f",
          }}
        >
          <div>
            <h1 style={{ margin: 0 }}>
              CANDLE LIVE BOT{" "}
              <span style={{ fontSize: 14, color: isArmed ? "#ff8f9a" : "#ffcf6b", marginLeft: 8 }}>
                {isArmed ? "● LIVE ARMED" : "○ PAPER (NOT ARMED)"}
              </span>
            </h1>
            <div className="sub">BTC/ETH candle strategy · Binance Spot execution · real USDT</div>
            <div
              style={{
                marginTop: 4,
                fontSize: 12,
                color: running ? "#63ffbe" : "#9fb2d3",
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <span
                style={{
                  display: "inline-block",
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: running ? "#63ffbe" : "#60708f",
                  boxShadow: running ? "0 0 10px #63ffbe" : "none",
                }}
              />
              {running ? "ENGINE RUNNING" : "ENGINE IDLE"} · {isArmed ? "REAL TRADES" : "PAPER UNTIL ARMED"}
            </div>
            {msg && <div style={{ marginTop: 4, fontSize: 12, color: "#8bc1ff" }}>{msg}</div>}
            {wallet?.live_error && (
              <div style={{ marginTop: 4, fontSize: 11, color: "#ff8f9a" }}>
                Binance: {wallet.live_error}
              </div>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end", alignItems: "center" }}>
            <label style={{ fontSize: 12, color: "#9fb2d3" }}>
              Strategy:
              <select
                value={strategyId}
                onChange={(e) => setStrategyId(e.target.value)}
                disabled={running}
                style={{ marginLeft: 8, background: "#121823", color: "#dfe6f5", border: "1px solid #5a2a3f", borderRadius: 6, padding: "6px 8px" }}
              >
                {(strategies.length ? strategies : [
                  { id: "sweep_bos_v1", label: "Sweep + BOS + volume (current)" },
                  { id: "smart_money_v2", label: "HTF S/D + CHOCH/BOS + RSI/Stoch + 3R" },
                ]).map((s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ))}
              </select>
            </label>
            {!isArmed && (
              <button className="btn btn-stop" disabled={busy} onClick={armLive}>
                Arm Live
              </button>
            )}
            {isArmed && (
              <button className="btn btn-secondary" disabled={busy} onClick={setPaper}>
                Set Paper
              </button>
            )}
            <button className="btn btn-start" disabled={busy || running} onClick={start}>
              Start
            </button>
            <button className="btn btn-stop" disabled={busy || !running} onClick={stop}>
              Stop
            </button>
            <button className="btn btn-secondary" disabled={busy} onClick={refresh}>
              Refresh
            </button>
            <button className="btn btn-stop" disabled={busy} onClick={killToggle}>
              {mode?.kill_switch ? "Disable Kill" : "Enable Kill"}
            </button>
          </div>
        </div>

        {/* KPI Grid */}
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
          <div className="operator-kpi"><span>MODE</span><strong>{isArmed ? "LIVE" : "PAPER"}</strong></div>
          <div className="operator-kpi"><span>STRATEGY</span><strong style={{ fontSize: 14 }}>{bot?.strategy_id ?? strategyId}</strong></div>
          <div className="operator-kpi"><span>USDT BAL</span><strong style={{ color: "#8bc1ff" }}>${balance.toFixed(2)}</strong></div>
          <div className="operator-kpi"><span>EQUITY</span><strong style={{ color: "#63ffbe" }}>${equity.toFixed(2)}</strong></div>
          <div className="operator-kpi">
            <span>TOTAL PNL</span>
            <strong style={{ color: totalPnl >= 0 ? "#63ffbe" : "#ff8f9a" }}>
              {totalPnl >= 0 ? "+" : ""}
              {totalPnl.toFixed(2)}
            </strong>
          </div>
          <div className="operator-kpi"><span>OPEN POS</span><strong>{bot?.active_positions ?? 0}</strong></div>
          <div className="operator-kpi"><span>TRADES TODAY</span><strong>{bot?.trades_today ?? 0}</strong></div>
          <div className="operator-kpi"><span>WIN RATE</span><strong style={{ color: "#63ffbe" }}>{winRate.toFixed(1)}%</strong></div>
          <div className="operator-kpi">
            <span>W / L</span>
            <strong style={{ fontSize: 18, whiteSpace: "nowrap" }}>
              <span style={{ color: "#63ffbe" }}>{wallet?.wins ?? 0}W</span> /{" "}
              <span style={{ color: "#ff8f9a" }}>{wallet?.losses ?? 0}L</span>
            </strong>
          </div>
          <div className="operator-kpi"><span>RISK/TRADE</span><strong>{((wallet?.risk_per_trade_pct ?? 0.01) * 100).toFixed(1)}%</strong></div>
          <div className="operator-kpi"><span>RR TARGET</span><strong>1:{(wallet?.rr_ratio ?? 2).toFixed(1)}</strong></div>
          <div className="operator-kpi">
            <span>DAILY LOSS</span>
            <strong style={{ color: (bot?.daily_loss_usd ?? 0) > 0 ? "#ff8f9a" : "#9fb2d3" }}>
              ${(bot?.daily_loss_usd ?? 0).toFixed(2)}
            </strong>
          </div>
          <div className="operator-kpi"><span>KILL SWITCH</span><strong style={{ color: mode?.kill_switch ? "#ff8f9a" : "#63ffbe" }}>{mode?.kill_switch ? "ON" : "OFF"}</strong></div>
        </div>

        {/* Symbol/Interval selector */}
        <div className="card" style={{ marginTop: 10, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ fontSize: 13, color: "#9fb2d3" }}>Symbol:</div>
          {(wallet?.symbols && wallet.symbols.length > 0 ? wallet.symbols : ["BTCUSDT", "ETHUSDT"]).map((s) => (
            <button
              key={s}
              className={s === symbol ? "btn btn-start" : "btn btn-secondary"}
              onClick={() => setSymbol(s)}
              style={{ padding: "4px 12px", fontSize: 12 }}
            >
              {s}
            </button>
          ))}
          <div style={{ fontSize: 13, color: "#9fb2d3", marginLeft: 12 }}>Interval:</div>
          {INTERVALS.map((iv) => (
            <button
              key={iv}
              className={iv === interval ? "btn btn-start" : "btn btn-secondary"}
              onClick={() => setInterval(iv)}
              style={{ padding: "4px 10px", fontSize: 12 }}
            >
              {iv}
            </button>
          ))}
          {signal && (
            <div style={{ marginLeft: "auto", display: "flex", gap: 12, fontSize: 12 }}>
              <span>
                Signal:{" "}
                <strong
                  style={{
                    color: signal.direction === "LONG" ? "#63ffbe" : signal.direction === "SHORT" ? "#ff8f9a" : "#9fb2d3",
                  }}
                >
                  {signal.direction}
                </strong>
              </span>
              <span>HTF: <strong>{signal.htf_bias}</strong></span>
              <span>Conf: <strong>{(signal.confidence * 100).toFixed(0)}%</strong></span>
              <span>RR: <strong>1:{signal.rr_ratio.toFixed(2)}</strong></span>
            </div>
          )}
        </div>

        {/* Chart */}
        <div className="card" style={{ marginTop: 10 }}>
          <CandleChart candles={candles} levels={levels} markers={markers} symbol={symbol} interval={interval} height={480} />
          {signal && signal.direction !== "SKIP" && (
            <div style={{ marginTop: 8, fontSize: 12, color: "#9fb2d3" }}>
              <strong style={{ color: "#8bc1ff" }}>Live Signal:</strong> {signal.reasoning}
            </div>
          )}
        </div>

        {/* Two-col: logs + trades */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1.2fr", gap: 10, marginTop: 10 }}>
          <div className="card" style={{ paddingBottom: 8 }}>
            <h3 style={{ marginTop: 0 }}>BOT LOGS</h3>
            <div
              style={{
                background: "#0b0f18",
                border: "1px solid #22314c",
                height: 360,
                overflowY: "auto",
                padding: 4,
              }}
            >
              {logs.length === 0 && <div style={{ padding: 8, color: "#6f86af", fontSize: 12 }}>No logs yet — arm and start the bot to see activity.</div>}
              {logs.map((l) => (
                <div
                  key={l.id}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "85px 56px 1fr",
                    gap: 8,
                    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                    fontSize: 11.5,
                    lineHeight: 1.45,
                    padding: "2px 4px",
                    borderBottom: "1px solid rgba(34,49,76,0.25)",
                  }}
                >
                  <span style={{ color: "#90a5cc" }}>{l.ts ? new Date(l.ts).toLocaleTimeString() : "--:--:--"}</span>
                  <span
                    style={{
                      color: l.level === "ERROR" ? "#ff8f9a" : l.level === "WARNING" ? "#ffcf6b" : "#8bc1ff",
                      fontWeight: 700,
                    }}
                  >
                    {l.level}
                  </span>
                  <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", color: "#d8e6ff" }}>{l.message}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="card" style={{ paddingBottom: 8 }}>
            <h3 style={{ marginTop: 0 }}>OPEN POSITIONS ({openTrades.length})</h3>
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: "#9fb2d3" }}>
                  <th style={{ textAlign: "left" }}>Symbol</th>
                  <th style={{ textAlign: "right" }}>Dir</th>
                  <th style={{ textAlign: "right" }}>Entry</th>
                  <th style={{ textAlign: "right" }}>SL</th>
                  <th style={{ textAlign: "right" }}>TP</th>
                  <th style={{ textAlign: "right" }}>Now</th>
                  <th style={{ textAlign: "right" }}>Unrl P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {openTrades.length === 0 && (
                  <tr>
                    <td colSpan={7} style={{ color: "#6f86af", padding: 8 }}>
                      No open positions
                    </td>
                  </tr>
                )}
                {openTrades.map((t) => {
                  const cur = t.current_price ?? t.entry_price;
                  const unrl = t.direction === "LONG" ? (cur - t.entry_price) * t.qty : (t.entry_price - cur) * t.qty;
                  return (
                    <tr key={t.id} style={{ borderBottom: "1px solid rgba(34,49,76,0.25)" }}>
                      <td>{t.symbol}</td>
                      <td style={{ textAlign: "right", color: t.direction === "LONG" ? "#63ffbe" : "#ff8f9a" }}>{t.direction}</td>
                      <td style={{ textAlign: "right" }}>{t.entry_price.toFixed(2)}</td>
                      <td style={{ textAlign: "right", color: "#ff8f9a" }}>{t.stop_loss.toFixed(2)}</td>
                      <td style={{ textAlign: "right", color: "#63ffbe" }}>{t.take_profit.toFixed(2)}</td>
                      <td style={{ textAlign: "right" }}>{cur.toFixed(2)}</td>
                      <td style={{ textAlign: "right", color: unrl >= 0 ? "#63ffbe" : "#ff8f9a" }}>{unrl >= 0 ? "+" : ""}{unrl.toFixed(2)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            <h3 style={{ marginTop: 12 }}>CLOSED TRADES (LIVE)</h3>
            <div style={{ maxHeight: 240, overflowY: "auto" }}>
              <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ color: "#9fb2d3", position: "sticky", top: 0, background: "#10131a" }}>
                    <th style={{ textAlign: "left" }}>Time</th>
                    <th style={{ textAlign: "left" }}>Sym</th>
                    <th style={{ textAlign: "right" }}>Dir</th>
                    <th style={{ textAlign: "right" }}>Entry</th>
                    <th style={{ textAlign: "right" }}>Exit</th>
                    <th style={{ textAlign: "right" }}>P&amp;L $</th>
                    <th style={{ textAlign: "right" }}>P&amp;L %</th>
                  </tr>
                </thead>
                <tbody>
                  {closedTrades.length === 0 && (
                    <tr>
                      <td colSpan={7} style={{ color: "#6f86af", padding: 8 }}>
                        No closed trades yet
                      </td>
                    </tr>
                  )}
                  {closedTrades.map((t) => (
                    <tr key={t.id} style={{ borderBottom: "1px solid rgba(34,49,76,0.25)" }}>
                      <td>{t.closed_at ? new Date(t.closed_at).toLocaleTimeString() : "--"}</td>
                      <td>{t.symbol}</td>
                      <td style={{ textAlign: "right", color: t.direction === "LONG" ? "#63ffbe" : "#ff8f9a" }}>{t.direction}</td>
                      <td style={{ textAlign: "right" }}>{t.entry_price.toFixed(2)}</td>
                      <td style={{ textAlign: "right" }}>{(t.exit_price ?? 0).toFixed(2)}</td>
                      <td style={{ textAlign: "right", color: t.pnl_usd >= 0 ? "#63ffbe" : "#ff8f9a" }}>
                        {t.pnl_usd >= 0 ? "+" : ""}
                        {t.pnl_usd.toFixed(2)}
                      </td>
                      <td style={{ textAlign: "right", color: t.pnl_pct >= 0 ? "#63ffbe" : "#ff8f9a" }}>
                        {t.pnl_pct >= 0 ? "+" : ""}
                        {t.pnl_pct.toFixed(2)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
