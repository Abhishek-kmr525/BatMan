"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";

type Trade = {
  id: string;
  market_id: string;
  market_title: string;
  category: string;
  direction: "YES" | "NO";
  amount: number;
  entry_price: number;
  exit_price: number | null;
  pnl: number;
  status: string;
  agent_score: number;
  source?: string;
  opened_at: string;
  closed_at?: string | null;
  time_to_close_seconds?: number | null;
};

type StatusFilter = "all" | "open" | "closed";
type SortKey =
  | "opened_at"
  | "closed_at"
  | "market_title"
  | "category"
  | "direction"
  | "status"
  | "source"
  | "agent_score"
  | "entry_price"
  | "exit_price"
  | "pnl";
type SortDir = "asc" | "desc";

function _slugify(v: string): string {
  return (v || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function isMockMarket(ticker: string): boolean {
  return ticker.toUpperCase().startsWith("MOCK-");
}

function kalshiMarketUrl(t: Trade): string {
  const ticker = (t.market_id || "").toLowerCase();
  if (!ticker) return "https://kalshi.com/markets";
  const eventTicker = ticker.split("-")[0] || ticker;
  const titleSlug = _slugify(t.market_title || ticker) || "market";
  return `https://kalshi.com/markets/${encodeURIComponent(eventTicker)}/${encodeURIComponent(titleSlug)}/${encodeURIComponent(ticker)}`;
}

export default function AllTradesPage() {
  const [rows, setRows] = useState<Trade[]>([]);
  const [status, setStatus] = useState<StatusFilter>("all");
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("opened_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [nowMs, setNowMs] = useState(Date.now());
  const [lastSyncMs, setLastSyncMs] = useState(Date.now());

  const limit = 100;

  async function refresh(currentStatus = status, currentPage = page) {
    setBusy(true);
    try {
      const data = await api<Trade[]>(
        `/trades?status=${currentStatus}&limit=${limit}&page=${currentPage}`,
      );
      setRows(data);
      setLastSyncMs(Date.now());
      setError(null);
    } catch (e: any) {
      setError(e?.message || "failed to load trades");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
    const id = setInterval(() => { void refresh(); }, 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  function onStatusChange(next: StatusFilter) {
    setStatus(next);
    setPage(1);
    void refresh(next, 1);
  }

  function goPrev() {
    const p = Math.max(1, page - 1);
    setPage(p);
    void refresh(status, p);
  }

  function goNext() {
    const p = page + 1;
    setPage(p);
    void refresh(status, p);
  }

  function applySort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDir(key === "opened_at" || key === "closed_at" ? "desc" : "asc");
  }

  function sortIndicator(key: SortKey) {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " ▲" : " ▼";
  }

  function num(v: number | null | undefined) {
    return v == null ? Number.NEGATIVE_INFINITY : v;
  }

  const sortedRows = [...rows].sort((a, b) => {
    let cmp = 0;
    switch (sortKey) {
      case "opened_at":
        cmp = new Date(a.opened_at).getTime() - new Date(b.opened_at).getTime();
        break;
      case "closed_at":
        cmp = new Date(a.closed_at || 0).getTime() - new Date(b.closed_at || 0).getTime();
        break;
      case "market_title":
        cmp = a.market_title.localeCompare(b.market_title);
        break;
      case "category":
        cmp = a.category.localeCompare(b.category);
        break;
      case "direction":
        cmp = a.direction.localeCompare(b.direction);
        break;
      case "status":
        cmp = a.status.localeCompare(b.status);
        break;
      case "source":
        cmp = (a.source || "").localeCompare(b.source || "");
        break;
      case "agent_score":
        cmp = a.agent_score - b.agent_score;
        break;
      case "entry_price":
        cmp = a.entry_price - b.entry_price;
        break;
      case "exit_price":
        cmp = num(a.exit_price) - num(b.exit_price);
        break;
      case "pnl":
        cmp = a.pnl - b.pnl;
        break;
      default:
        cmp = 0;
    }
    return sortDir === "asc" ? cmp : -cmp;
  });

  function fmtCountdown(t: Trade): string {
    if (t.status !== "OPEN") return "—";
    if (t.time_to_close_seconds == null) return "—";
    const elapsed = Math.floor((nowMs - lastSyncMs) / 1000);
    const left = Math.max(0, t.time_to_close_seconds - elapsed);
    const m = Math.floor(left / 60);
    const s = left % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  return (
    <div className="container">
      <h1>All Trades</h1>
      <div className="sub">Complete trade ledger from database: active + closed positions.</div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <label className="sub" style={{ margin: 0 }}>
            status:&nbsp;
            <select
              value={status}
              onChange={(e) => onStatusChange(e.target.value as StatusFilter)}
              disabled={busy}
            >
              <option value="all">all</option>
              <option value="open">open</option>
              <option value="closed">closed</option>
            </select>
          </label>
          <button className="btn btn-secondary" onClick={() => void refresh()} disabled={busy}>
            Refresh
          </button>
          <span className="sub" style={{ margin: 0 }}>
            page {page} · {rows.length} rows
          </span>
          <button className="btn btn-secondary" onClick={goPrev} disabled={busy || page <= 1}>
            Prev
          </button>
          <button className="btn btn-secondary" onClick={goNext} disabled={busy || rows.length < limit}>
            Next
          </button>
        </div>
        {error && (
          <div className="sub" style={{ marginTop: 10, color: "#ff7b7b" }}>
            {error}
          </div>
        )}
      </div>

      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("opened_at")}>Opened{sortIndicator("opened_at")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("closed_at")}>Closed{sortIndicator("closed_at")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("market_title")}>Market{sortIndicator("market_title")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("category")}>Cat{sortIndicator("category")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("direction")}>Side{sortIndicator("direction")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("status")}>Status{sortIndicator("status")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("source")}>Source{sortIndicator("source")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("agent_score")}>Score{sortIndicator("agent_score")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("entry_price")}>Entry{sortIndicator("entry_price")}</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("exit_price")}>Exit{sortIndicator("exit_price")}</th>
              <th>Result In</th>
              <th style={{ cursor: "pointer" }} onClick={() => applySort("pnl")}>P&L{sortIndicator("pnl")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={12} className="sub">No trades found.</td>
              </tr>
            )}
            {sortedRows.map((t) => (
              <tr key={t.id}>
                <td className="sub">{new Date(t.opened_at).toLocaleString()}</td>
                <td className="sub">{t.closed_at ? new Date(t.closed_at).toLocaleString() : "—"}</td>
                <td title={t.market_title}>
                  {isMockMarket(t.market_id) ? (
                    <a
                      href={`/quick?market_id=${encodeURIComponent(t.market_id)}`}
                      style={{ color: "#6ea8fe", textDecoration: "none" }}
                    >
                      {t.market_title.slice(0, 60)}
                    </a>
                  ) : (
                    <a
                      href={kalshiMarketUrl(t)}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: "#6ea8fe", textDecoration: "none" }}
                    >
                      {t.market_title.slice(0, 60)}
                    </a>
                  )}
                </td>
                <td className="sub">{t.category}</td>
                <td>
                  <span className={`badge badge-${t.direction === "YES" ? "yes" : "no"}`}>
                    {t.direction}
                  </span>
                </td>
                <td className="sub">{t.status}</td>
                <td className="sub">{t.source || "—"}</td>
                <td>{t.agent_score}</td>
                <td>{t.entry_price.toFixed(2)}</td>
                <td>{t.exit_price != null ? t.exit_price.toFixed(2) : "—"}</td>
                <td className="sub">{fmtCountdown(t)}</td>
                <td className={t.pnl >= 0 ? "pos" : "neg"}>
                  {t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(4)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
