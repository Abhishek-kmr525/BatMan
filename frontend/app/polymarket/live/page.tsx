"use client";

import { useEffect, useState } from "react";
import { api } from "../../../lib/api";

type ModeState = {
  platform: string;
  mode: "paper" | "live_requested" | "live_armed";
  live_enabled: boolean;
  kill_switch: boolean;
  requested_at: string | null;
  armed_at: string | null;
  last_changed_at: string;
  limits: Record<string, unknown>;
};

type ModeSnapshot = {
  kalshi: ModeState;
  polymarket: ModeState;
};

type Wallet = {
  balance: number;
  total_pnl: number;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  live_error?: string | null;
};

type BotStatus = {
  status: string;
  state: string;
  active_positions: number;
  trades_today: number;
  scanned_markets_today: number;
  last_candidate_count: number;
  mode_guard?: { mode: string; live_enabled: boolean };
};

type Trade = {
  id: string;
  market_title: string;
  direction: "YES" | "NO";
  amount: number;
  entry_price: number;
  current_price?: number | null;
  pnl: number;
  status: string;
  agent_score: number;
  opened_at: string;
  closed_at?: string | null;
};

function badgeForMode(m: ModeState["mode"]) {
  if (m === "live_armed") return { text: "🔴 LIVE ARMED", cls: "neg" };
  if (m === "live_requested") return { text: "🟡 LIVE REQUESTED", cls: "pending" };
  return { text: "🟢 PAPER", cls: "pos" };
}

export default function PolymarketLivePage() {
  const [mode, setMode] = useState<ModeState | null>(null);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [recentClosed, setRecentClosed] = useState<Trade[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [passcode, setPasscode] = useState("");
  const [maxOrderUsd, setMaxOrderUsd] = useState<string>("2");
  const [maxTradesPerDay, setMaxTradesPerDay] = useState<string>("5");
  const [maxExposureUsd, setMaxExposureUsd] = useState<string>("10");

  async function refresh() {
    try {
      const [snap, w, b, openT, closedT] = await Promise.all([
        api<ModeSnapshot>("/mode/status"),
        api<Wallet>("/polymarket/wallet"),
        api<BotStatus>("/polymarket/bot/status"),
        api<Trade[]>("/polymarket/trades?status=open&limit=50&page=1"),
        api<Trade[]>("/polymarket/trades?status=closed&limit=20&page=1"),
      ]);
      setMode(snap.polymarket);
      setWallet(w);
      setBot(b);
      setOpenTrades(openT);
      setRecentClosed(closedT);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "refresh failed");
    }
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 4000);
    return () => clearInterval(id);
  }, []);

  function flash(msg: string) {
    setInfo(msg);
    setTimeout(() => setInfo(null), 3500);
  }

  async function requestLive() {
    setBusy(true);
    try {
      await api("/mode/request-live", {
        method: "POST",
        body: JSON.stringify({ platform: "polymarket" }),
      });
      flash("Live mode requested. Enter passcode and confirm.");
      await refresh();
    } catch (e: any) {
      alert("Request failed: " + e.message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmLive() {
    if (!passcode) {
      alert("Enter passcode first");
      return;
    }
    setBusy(true);
    try {
      const limits: Record<string, number> = {};
      const a = parseFloat(maxOrderUsd);
      const b = parseInt(maxTradesPerDay, 10);
      const c = parseFloat(maxExposureUsd);
      if (!Number.isNaN(a)) limits.max_order_usd = a;
      if (!Number.isNaN(b)) limits.max_new_trades_per_day = b;
      if (!Number.isNaN(c)) limits.max_total_exposure_usd = c;
      await api("/mode/confirm-live", {
        method: "POST",
        body: JSON.stringify({ platform: "polymarket", passcode, limits }),
      });
      setPasscode("");
      flash("Live mode armed. Bot will trade real USDC.");
      await refresh();
    } catch (e: any) {
      alert("Confirm failed: " + e.message);
    } finally {
      setBusy(false);
    }
  }

  async function disarm() {
    if (!confirm("Disarm live mode and switch back to paper?")) return;
    setBusy(true);
    try {
      await api("/mode/set-paper", {
        method: "POST",
        body: JSON.stringify({ platform: "polymarket" }),
      });
      flash("Switched to paper mode.");
      await refresh();
    } catch (e: any) {
      alert("Disarm failed: " + e.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleKillSwitch() {
    if (!mode) return;
    const next = !mode.kill_switch;
    if (next && !confirm("Enable kill switch? This stops all new trades and forces paper mode.")) return;
    setBusy(true);
    try {
      await api("/mode/kill-switch", {
        method: "POST",
        body: JSON.stringify({ platform: "polymarket", enabled: next }),
      });
      flash(next ? "Kill switch ENABLED." : "Kill switch disabled.");
      await refresh();
    } catch (e: any) {
      alert("Kill switch failed: " + e.message);
    } finally {
      setBusy(false);
    }
  }

  async function startBot() {
    setBusy(true);
    try {
      await api("/polymarket/bot/start", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  async function stopBot() {
    setBusy(true);
    try {
      await api("/polymarket/bot/stop", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  const badge = mode ? badgeForMode(mode.mode) : null;
  const isLive = mode?.mode === "live_armed";
  const isRequested = mode?.mode === "live_requested";
  const botRunning = bot?.status === "running";

  return (
    <div className="container dashx">
      <div className="dashx-top">
        <div>
          <h1>🔴 Polymarket Live Mode</h1>
          <div className="sub">Arm, monitor, and disarm real-money trading on Polymarket</div>
        </div>
        <div className="dashx-actions">
          {badge && (
            <span className={`dashx-hero-badge ${badge.cls}`} style={{ fontSize: 14, padding: "6px 12px" }}>
              <span className="dot" />
              {badge.text}
            </span>
          )}
          {!botRunning ? (
            <button className="btn btn-start" onClick={startBot} disabled={busy}>▶ Start Bot</button>
          ) : (
            <button className="btn btn-stop" onClick={stopBot} disabled={busy}>■ Stop Bot</button>
          )}
          <button className="btn btn-secondary" onClick={refresh} disabled={busy}>Refresh</button>
        </div>
      </div>

      {error && (
        <div className="sub" style={{ marginTop: 8, color: "#ff7b7b", marginBottom: 8 }}>
          Refresh issue: {error}
        </div>
      )}
      {info && (
        <div className="sub" style={{ marginTop: 8, color: "#7bff9b", marginBottom: 8 }}>
          {info}
        </div>
      )}

      <div className="dashx-hero-row">
        <div className="card dashx-hero">
          <div className="dashx-hero-label">Live Wallet (USDC)</div>
          <div className="dashx-hero-value">
            {isLive ? `$${(wallet?.balance ?? 0).toFixed(2)}` : "—"}
            <span className="dashx-hero-delta muted">
              {isLive ? "on-chain CLOB" : "arm to view"}
            </span>
          </div>
          {wallet?.live_error && (
            <div className="sub" style={{ color: "#ff7b7b", marginTop: 4 }}>{wallet.live_error}</div>
          )}
        </div>

        <div className="card dashx-hero">
          <div className="dashx-hero-label">Active Live Positions</div>
          <div className="dashx-hero-value">
            {isLive ? (bot?.active_positions ?? 0) : 0}
            <span className="dashx-hero-delta muted">
              {bot?.last_candidate_count ?? 0} candidates
            </span>
          </div>
        </div>

        <div className="card dashx-hero">
          <div className="dashx-hero-label">Live P&amp;L (closed)</div>
          <div className={`dashx-hero-value ${(wallet?.total_pnl ?? 0) >= 0 ? "pos" : "neg"}`}>
            {isLive
              ? `${(wallet?.total_pnl ?? 0) >= 0 ? "+" : "-"}$${Math.abs(wallet?.total_pnl ?? 0).toFixed(2)}`
              : "—"}
            <span className="dashx-hero-delta muted">
              {isLive ? `${wallet?.total_trades ?? 0} trades` : "arm to view"}
            </span>
          </div>
        </div>

        <div className="card dashx-hero">
          <div className="dashx-hero-label">Win Rate (live)</div>
          <div className="dashx-hero-value">
            {isLive ? `${(wallet?.win_rate ?? 0).toFixed(1)}%` : "—"}
            <span className="dashx-hero-delta muted">
              {isLive ? `${wallet?.wins ?? 0}W / ${wallet?.losses ?? 0}L` : ""}
            </span>
          </div>
        </div>
      </div>

      <div className="dashx-grid-mid">
        <div className="dashx-main">
          <div className="card" style={{ padding: 16 }}>
            <h3 style={{ marginTop: 0 }}>Arming Controls</h3>
            {!mode?.live_enabled && (
              <div className="sub" style={{ color: "#ffae6b", marginBottom: 12 }}>
                ⚠ Live mode is disabled in config (POLYMARKET_LIVE_ENABLED=false).
              </div>
            )}
            {mode?.kill_switch && (
              <div className="sub" style={{ color: "#ff7b7b", marginBottom: 12 }}>
                🛑 Kill switch is ENABLED. Disable it before arming live.
              </div>
            )}

            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginBottom: 12 }}>
              <div>
                <label className="sub">Max order USD</label>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  value={maxOrderUsd}
                  onChange={(e) => setMaxOrderUsd(e.target.value)}
                  disabled={busy || isLive}
                  style={{ width: "100%" }}
                />
              </div>
              <div>
                <label className="sub">Max new trades / day</label>
                <input
                  className="input"
                  type="number"
                  step="1"
                  value={maxTradesPerDay}
                  onChange={(e) => setMaxTradesPerDay(e.target.value)}
                  disabled={busy || isLive}
                  style={{ width: "100%" }}
                />
              </div>
              <div>
                <label className="sub">Max total exposure USD</label>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  value={maxExposureUsd}
                  onChange={(e) => setMaxExposureUsd(e.target.value)}
                  disabled={busy || isLive}
                  style={{ width: "100%" }}
                />
              </div>
            </div>

            {!isLive && !isRequested && (
              <button
                className="btn btn-stop"
                onClick={requestLive}
                disabled={busy || !mode?.live_enabled || !!mode?.kill_switch}
              >
                Request Live Mode
              </button>
            )}

            {isRequested && (
              <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
                <div style={{ flex: "1 1 200px" }}>
                  <label className="sub">Confirm passcode</label>
                  <input
                    className="input"
                    type="password"
                    value={passcode}
                    onChange={(e) => setPasscode(e.target.value)}
                    placeholder="••••"
                    style={{ width: "100%" }}
                    autoFocus
                  />
                </div>
                <button className="btn btn-stop" onClick={confirmLive} disabled={busy || !passcode}>
                  Arm Live
                </button>
                <button className="btn btn-secondary" onClick={disarm} disabled={busy}>
                  Cancel
                </button>
              </div>
            )}

            {isLive && (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button className="btn btn-secondary" onClick={disarm} disabled={busy}>
                  Disarm (back to paper)
                </button>
                <button className="btn btn-stop" onClick={toggleKillSwitch} disabled={busy}>
                  🛑 Kill Switch
                </button>
              </div>
            )}

            {!isLive && (
              <div style={{ marginTop: 12 }}>
                <button
                  className={`btn ${mode?.kill_switch ? "btn-start" : "btn-secondary"}`}
                  onClick={toggleKillSwitch}
                  disabled={busy}
                >
                  {mode?.kill_switch ? "Disable Kill Switch" : "🛑 Enable Kill Switch"}
                </button>
              </div>
            )}
          </div>

          <div className="card" style={{ marginTop: 12 }}>
            <h3 style={{ marginTop: 0 }}>Open Live Positions</h3>
            {!isLive && <div className="sub">Arm live mode to view positions.</div>}
            {isLive && openTrades.length === 0 && <div className="sub">No open live positions.</div>}
            {isLive && openTrades.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>Market</th>
                    <th>Dir</th>
                    <th>Entry</th>
                    <th>Now</th>
                    <th>Size $</th>
                    <th>Score</th>
                    <th>Opened</th>
                  </tr>
                </thead>
                <tbody>
                  {openTrades.map((t) => (
                    <tr key={t.id}>
                      <td title={t.market_title}>{t.market_title.slice(0, 60)}</td>
                      <td>{t.direction}</td>
                      <td>{t.entry_price.toFixed(3)}</td>
                      <td>{(t.current_price ?? 0).toFixed(3)}</td>
                      <td>${t.amount.toFixed(2)}</td>
                      <td>{t.agent_score}</td>
                      <td>{new Date(t.opened_at).toLocaleTimeString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="card" style={{ marginTop: 12 }}>
            <h3 style={{ marginTop: 0 }}>Recent Closed (live)</h3>
            {!isLive && <div className="sub">Arm live mode to view closed trades.</div>}
            {isLive && recentClosed.length === 0 && <div className="sub">No closed live trades yet.</div>}
            {isLive && recentClosed.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>Market</th>
                    <th>Dir</th>
                    <th>Entry → Exit</th>
                    <th>P&amp;L</th>
                    <th>Status</th>
                    <th>Closed</th>
                  </tr>
                </thead>
                <tbody>
                  {recentClosed.map((t) => (
                    <tr key={t.id}>
                      <td title={t.market_title}>{t.market_title.slice(0, 60)}</td>
                      <td>{t.direction}</td>
                      <td>
                        {t.entry_price.toFixed(3)} → {(t.current_price ?? 0).toFixed(3)}
                      </td>
                      <td className={t.pnl >= 0 ? "pos" : "neg"}>
                        {t.pnl >= 0 ? "+" : ""}
                        {t.pnl.toFixed(4)}
                      </td>
                      <td>{t.status.replace("CLOSED_", "")}</td>
                      <td>{t.closed_at ? new Date(t.closed_at).toLocaleString() : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="card dashx-side">
          <h3 style={{ marginTop: 0 }}>Mode Telemetry</h3>
          <div className="dashx-account-row">
            <span>Mode</span>
            <strong className={badge?.cls === "neg" ? "neg" : badge?.cls === "pending" ? "pending" : "pos"}>
              {mode?.mode ?? "—"}
            </strong>
          </div>
          <div className="dashx-account-row">
            <span>Live enabled (config)</span>
            <strong>{mode?.live_enabled ? "yes" : "no"}</strong>
          </div>
          <div className="dashx-account-row">
            <span>Kill switch</span>
            <strong className={mode?.kill_switch ? "neg" : "pos"}>
              {mode?.kill_switch ? "ON" : "off"}
            </strong>
          </div>
          <div className="dashx-account-row">
            <span>Requested at</span>
            <strong>{mode?.requested_at ? new Date(mode.requested_at).toLocaleString() : "—"}</strong>
          </div>
          <div className="dashx-account-row">
            <span>Armed at</span>
            <strong>{mode?.armed_at ? new Date(mode.armed_at).toLocaleString() : "—"}</strong>
          </div>
          <div className="dashx-account-row">
            <span>Last changed</span>
            <strong>{mode?.last_changed_at ? new Date(mode.last_changed_at).toLocaleString() : "—"}</strong>
          </div>
          <div className="dashx-account-row">
            <span>Bot</span>
            <strong>
              {bot?.status ?? "—"} · {bot?.state ?? "—"}
            </strong>
          </div>
          <div className="dashx-account-row">
            <span>Trades today</span>
            <strong>{bot?.trades_today ?? 0}</strong>
          </div>
          <div className="dashx-account-row">
            <span>Scanned today</span>
            <strong>{bot?.scanned_markets_today ?? 0}</strong>
          </div>

          <h3 style={{ marginTop: 16 }}>Active Limits</h3>
          {mode?.limits && Object.keys(mode.limits).length > 0 ? (
            Object.entries(mode.limits).map(([k, v]) => (
              <div className="dashx-account-row" key={k}>
                <span>{k}</span>
                <strong>{String(v)}</strong>
              </div>
            ))
          ) : (
            <div className="sub">No overrides — using config defaults.</div>
          )}
        </div>
      </div>
    </div>
  );
}
