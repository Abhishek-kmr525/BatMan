"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";

type Market = {
  ticker: string; title: string; category: string;
  yes_price: number; no_price: number; volume: number;
  time_to_close_seconds: number;
};

type Analysis = {
  score: number; action: string; confidence: number;
  entry_price: number; target_exit_price: number; stop_loss_price: number;
  reasoning: string; knowledge_sources: string[];
};

export default function QuickTradePage() {
  const [markets, setMarkets] = useState<any[]>([]);
  const [filter, setFilter] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ market: Market; analysis: Analysis } | null>(null);
  const [direction, setDirection] = useState<"" | "YES" | "NO">("");
  const [override, setOverride] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const lastAutoPreview = useRef<string | null>(null);

  useEffect(() => {
    api<any[]>("/markets?limit=80").then(setMarkets).catch(() => setMarkets([]));
  }, []);

  const filtered = markets.filter(m =>
    !filter ||
    m.ticker.toLowerCase().includes(filter.toLowerCase()) ||
    (m.title || "").toLowerCase().includes(filter.toLowerCase())
  ).slice(0, 25);

  async function runPreview(ticker: string) {
    setPicked(ticker);
    setPreview(null);
    setResult(null);
    setBusy(true);
    try {
      const r = await api<{ market: Market; analysis: Analysis }>(
        "/quick-trade/preview",
        { method: "POST", body: JSON.stringify({ market_id: ticker }) },
      );
      setPreview(r);
      // default the dropdown to the analyzer's suggestion
      if (r.analysis.action === "BUY_YES") setDirection("YES");
      else if (r.analysis.action === "BUY_NO") setDirection("NO");
      else setDirection("");
    } catch (e: any) {
      setResult(`Preview failed: ${e?.message || e}`);
    } finally { setBusy(false); }
  }

  async function execute() {
    if (!picked) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await api<any>("/quick-trade/execute", {
        method: "POST",
        body: JSON.stringify({
          market_id: picked,
          direction: direction || null,
          override,
        }),
      });
      setResult(`✅ Opened ${r.direction} ${r.market_id} @ $${r.entry_price.toFixed(2)} (score ${r.agent_score})`);
    } catch (e: any) {
      setResult(`❌ ${e?.message || e}`);
    } finally { setBusy(false); }
  }

  useEffect(() => {
    const deepLinkMarketId = new URLSearchParams(window.location.search).get("market_id")?.trim() || "";
    if (!deepLinkMarketId) return;
    if (lastAutoPreview.current === deepLinkMarketId) return;
    lastAutoPreview.current = deepLinkMarketId;
    setFilter(deepLinkMarketId);
    void runPreview(deepLinkMarketId);
  }, []);

  return (
    <div className="container">
      <h1>Quick Trade</h1>
      <div className="sub">
        Pick a market, see the AI analysis, optionally override direction or threshold, then open a $1 paper position.
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <div className="card" style={{ flexBasis: 460 }}>
          <h3>1. Choose market</h3>
          <input
            type="text"
            placeholder="filter by ticker or title…"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            style={{ width: "100%", marginBottom: 10 }}
          />
          <div style={{ maxHeight: 380, overflowY: "auto" }}>
            {filtered.length === 0 && <div className="sub">No markets match.</div>}
            {filtered.map(m => (
              <div
                key={m.ticker}
                onClick={() => runPreview(m.ticker)}
                style={{
                  padding: "8px 10px",
                  cursor: "pointer",
                  borderRadius: 6,
                  background: picked === m.ticker ? "#1a2030" : "transparent",
                  borderBottom: "1px solid #1a2030",
                }}
              >
                <div style={{ fontSize: 13 }}>{(m.title || m.ticker).slice(0, 80)}</div>
                <div className="sub" style={{ fontSize: 11 }}>
                  YES ${m.yes_price?.toFixed(2)} · vol {m.volume} · closes in {Math.floor((m.time_to_close_seconds||0)/60)}m
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="card" style={{ flexBasis: 600 }}>
          <h3>2. AI analysis</h3>
          {!preview ? (
            <div className="sub">Pick a market on the left.</div>
          ) : (
            <>
              <div style={{ fontWeight: 600 }}>{preview.market.title}</div>
              <div className="sub" style={{ marginTop: 4 }}>
                YES ${preview.market.yes_price.toFixed(2)} / NO ${preview.market.no_price.toFixed(2)} ·
                vol {preview.market.volume} · closes in {Math.floor(preview.market.time_to_close_seconds/60)}m
              </div>
              <div style={{ marginTop: 14, display: "flex", gap: 12, flexWrap: "wrap" }}>
                <span className="badge badge-yes">score {preview.analysis.score}</span>
                <span className="badge badge-yes">{preview.analysis.action}</span>
                <span className="badge badge-stopped">conf {preview.analysis.confidence.toFixed(2)}</span>
                <span className="sub">target ${preview.analysis.target_exit_price.toFixed(2)}</span>
                <span className="sub">stop ${preview.analysis.stop_loss_price.toFixed(2)}</span>
              </div>
              <div style={{ marginTop: 12, fontSize: 13, color: "#c0c5d0" }}>
                {preview.analysis.reasoning}
              </div>
              {preview.analysis.knowledge_sources?.length > 0 && (
                <div className="sub" style={{ marginTop: 8, fontSize: 11 }}>
                  Sources: {preview.analysis.knowledge_sources.slice(0, 4).join(" · ")}
                </div>
              )}

              <h3 style={{ marginTop: 22 }}>3. Open position</h3>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <label className="sub">direction:&nbsp;
                  <select value={direction} onChange={e => setDirection(e.target.value as any)}>
                    <option value="">(use AI)</option>
                    <option value="YES">YES</option>
                    <option value="NO">NO</option>
                  </select>
                </label>
                <label className="sub" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <input type="checkbox" checked={override} onChange={e => setOverride(e.target.checked)} />
                  override score gate
                </label>
                <button className="btn btn-start" onClick={execute} disabled={busy}>Open $1 paper trade</button>
              </div>
              {result && <div className="sub" style={{ marginTop: 12, fontSize: 13 }}>{result}</div>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
