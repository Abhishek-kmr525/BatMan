"""Polymarket-specific AI analyzer for short-window UP/DOWN crypto markets.

Pipeline:
1. Detect the asset & interval from the market title/slug (BTC/ETH/SOL/XRP/DOGE/HYPE/BNB,
   5m or 15m).
2. Fetch recent candles for that asset from Binance (with Coinbase fallback).
3. Compute deterministic technical features (last close, % change over window,
   recent range, momentum, candle pattern).
4. Pull cheat-sheet chunks from the knowledge base via knowledge.query().
5. If ANTHROPIC_API_KEY is set, ask Claude for a JSON decision. Otherwise fall
   back to a deterministic candle-based score.
6. Return PolyAnalysis with score, action (BUY_YES / BUY_NO / SKIP), reasoning,
   and the knowledge sources we leaned on.

Intentionally separate from the Kalshi analyzer to keep the prompt + features
focused on the 5m/15m up-down playbook.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
from anthropic import Anthropic

from app.agent import knowledge
from app.core.config import settings
from app.services.polymarket import PolyMarket


_ASSET_TOKENS: dict[str, list[str]] = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth"],
    "SOLUSDT": ["solana", "sol"],
    "XRPUSDT": ["xrp"],
    "DOGEUSDT": ["dogecoin", "doge"],
    "BNBUSDT": ["bnb"],
    "HYPEUSDT": ["hype", "hyperliquid"],
}

_COINBASE_PRODUCT: dict[str, str] = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "SOLUSDT": "SOL-USD",
    "XRPUSDT": "XRP-USD",
    "DOGEUSDT": "DOGE-USD",
    "BNBUSDT": "BNB-USD",
    "HYPEUSDT": "HYPE-USD",
}

_INTERVAL_PATTERNS = [
    (re.compile(r"(\b|-)5m(\b|-|_)"), "5m"),
    (re.compile(r"(\b|-)15m(\b|-|_)"), "15m"),
    (re.compile(r"5\s*min"), "5m"),
    (re.compile(r"15\s*min"), "15m"),
]


@dataclass
class PolyAnalysis:
    score: int
    action: str  # BUY_YES | BUY_NO | SKIP
    confidence: float
    entry_price: float
    reasoning: str
    knowledge_sources: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)
    asset: str | None = None
    interval: str | None = None


# ---------- Detection ----------

def detect_asset_and_interval(market: PolyMarket) -> tuple[str | None, str | None]:
    title = (market.title or "").lower()
    slug = str((market.raw or {}).get("slug") or "").lower()
    hay = f"{title} {slug}"

    asset: str | None = None
    for sym, toks in _ASSET_TOKENS.items():
        if any(tok in hay for tok in toks):
            asset = sym
            break

    interval: str | None = None
    for pat, label in _INTERVAL_PATTERNS:
        if pat.search(hay):
            interval = label
            break
    return asset, interval


# ---------- Candle fetch ----------

async def fetch_candles(symbol: str, interval: str, limit: int = 60) -> tuple[list[dict], str]:
    """Returns (candles, source). source is 'binance' | 'coinbase' | 'none'."""
    if interval not in {"5m", "15m"}:
        return [], "none"
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as h:
            r = await h.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            r.raise_for_status()
            for row in r.json():
                out.append({
                    "t": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
        return out, "binance"
    except Exception:
        pass

    product = _COINBASE_PRODUCT.get(symbol)
    if not product:
        return [], "none"
    granularity = 300 if interval == "5m" else 900
    try:
        async with httpx.AsyncClient(timeout=10.0) as h:
            r = await h.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={"granularity": granularity},
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            rows = r.json() or []
        rows = rows[:limit]
        for row in reversed(rows):
            out.append({
                "t": int(row[0]) * 1000,
                "open": float(row[3]),
                "high": float(row[2]),
                "low": float(row[1]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        return out, "coinbase"
    except Exception:
        return [], "none"


# ---------- Feature engineering ----------

def candle_features(candles: list[dict]) -> dict[str, Any]:
    if not candles:
        return {"available": False}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    last = closes[-1]
    open0 = candles[0]["open"]
    pct_window = (last - open0) / open0 * 100 if open0 else 0.0

    # Last 6 candles trend
    recent = closes[-6:] if len(closes) >= 6 else closes
    pct_recent = (recent[-1] - recent[0]) / recent[0] * 100 if recent[0] else 0.0
    ups = sum(1 for c in candles[-6:] if c["close"] >= c["open"])
    downs = sum(1 for c in candles[-6:] if c["close"] < c["open"])

    rng = max(highs) - min(lows) if highs else 0.0
    avg_close = sum(closes) / len(closes)
    avg_vol = sum(volumes) / len(volumes) if volumes else 0.0
    last_vol = volumes[-1]
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 1.0

    last_candle = candles[-1]
    body = last_candle["close"] - last_candle["open"]
    body_pct = (body / last_candle["open"] * 100) if last_candle["open"] else 0.0

    return {
        "available": True,
        "last_close": round(last, 4),
        "open_at_window_start": round(open0, 4),
        "pct_change_window": round(pct_window, 4),
        "pct_change_last6": round(pct_recent, 4),
        "ups_last6": ups,
        "downs_last6": downs,
        "range_window": round(rng, 4),
        "avg_close": round(avg_close, 4),
        "vol_ratio": round(vol_ratio, 4),
        "last_body_pct": round(body_pct, 4),
        "candles_used": len(candles),
    }


# ---------- Knowledge ----------

def cheatsheet_chunks(asset: str | None, interval: str | None) -> list[dict]:
    parts: list[str] = ["polymarket up or down", "5m 15m crypto cheatsheet", "short window momentum"]
    if asset:
        parts.append(asset.replace("USDT", ""))
    if interval:
        parts.append(interval)
    query = " ".join(parts)
    try:
        return knowledge.query(query, k=5) or []
    except Exception:
        return []


def _format_chunks(chunks: Iterable[dict]) -> str:
    blocks = []
    for c in chunks:
        meta = c.get("metadata") or {}
        src = f"{meta.get('file_name','?')} p.{meta.get('page','?')}"
        blocks.append(f"[{src}] {c.get('text','')[:600]}")
    return "\n\n".join(blocks) if blocks else "(no cheatsheet context)"


# ---------- Heuristic fallback ----------

def heuristic_decision(
    market: PolyMarket,
    feats: dict[str, Any],
) -> tuple[int, str, float, str]:
    """Returns (score, action, confidence, reasoning)."""
    if not feats.get("available"):
        # No candles → small edge from price only.
        side = "YES" if market.yes_price <= market.no_price else "NO"
        edge = max(0.0, 0.5 - (market.yes_price if side == "YES" else market.no_price))
        score = int(round(min(80, 45 + edge * 70)))
        action = "BUY_YES" if side == "YES" else "BUY_NO"
        return score, action, 0.45, f"no candles; edge-only side={side} edge={edge:.2f}"

    pct = feats.get("pct_change_window", 0.0)
    pct_recent = feats.get("pct_change_last6", 0.0)
    ups = feats.get("ups_last6", 0)
    downs = feats.get("downs_last6", 0)
    vol_ratio = feats.get("vol_ratio", 1.0)
    body = feats.get("last_body_pct", 0.0)

    # Direction blend: window trend + recent momentum + last candle body.
    direction = 0.5 * pct + 0.3 * pct_recent + 0.2 * body
    if direction >= 0:
        action = "BUY_YES"  # YES = "Up"
    else:
        action = "BUY_NO"   # NO = "Down"

    strength = min(1.0, abs(direction) / 0.6 + abs(ups - downs) * 0.08)
    vol_boost = min(0.2, max(0.0, (vol_ratio - 1.0) * 0.1))
    confidence = round(min(0.95, 0.45 + 0.4 * strength + vol_boost), 4)

    # Score in 0..100. Floor at 50 so very weak signals are still ranked.
    score = int(round(50 + 45 * strength + 5 * (vol_boost / 0.2)))
    score = max(0, min(99, score))

    # Penalize if implied price already strongly disagrees with our direction.
    yes = market.yes_price
    if action == "BUY_YES" and yes > 0.7:
        score -= 8
    if action == "BUY_NO" and yes < 0.3:
        score -= 8

    reasoning = (
        f"win%={pct:+.2f}% recent6={pct_recent:+.2f}% bias={direction:+.3f} "
        f"ups={ups}/downs={downs} vol×{vol_ratio:.2f} body={body:+.2f}%"
    )
    return max(0, score), action, confidence, reasoning


# ---------- Claude prompt ----------

POLY_SYSTEM_PROMPT = """You are AMTA, an AI Master Trading Agent specialized in
short-window (5m/15m) Polymarket UP/DOWN crypto contracts.

You receive: market metadata, recent candles features for the underlying asset,
and cheatsheet excerpts from PDFs in our knowledge base.

Your job: decide whether to bet YES (price goes up by close) or NO (down), or SKIP.

Hard rules:
- Position size is fixed at $1.00. Do not propose larger sizes.
- action must be one of: BUY_YES, BUY_NO, SKIP.
- Only return BUY_* if score >= 65 AND confidence >= 0.55.
- entry_price must equal yes_price for BUY_YES, no_price for BUY_NO.
- Output STRICT JSON only — no prose, no markdown.

Schema:
{
  "score": int 0-100,
  "action": "BUY_YES" | "BUY_NO" | "SKIP",
  "confidence": float 0..1,
  "entry_price": float 0..1,
  "reasoning": str (<= 240 chars),
  "knowledge_sources": [str]
}
"""


def _build_user_prompt(
    market: PolyMarket,
    feats: dict[str, Any],
    candles: list[dict],
    chunks: list[dict],
    asset: str | None,
    interval: str | None,
) -> str:
    market_json = json.dumps({
        "id": market.id,
        "title": market.title,
        "slug": (market.raw or {}).get("slug"),
        "yes_price": market.yes_price,
        "no_price": market.no_price,
        "volume": market.volume,
        "time_to_close_seconds": market.close_time_seconds,
        "asset": asset,
        "interval": interval,
    }, indent=2)
    feats_json = json.dumps(feats, indent=2)
    last_candles = candles[-12:] if candles else []
    candles_json = json.dumps([
        {"t": c["t"], "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"], "v": c["volume"]}
        for c in last_candles
    ], indent=2)
    return (
        f"MARKET:\n{market_json}\n\n"
        f"FEATURES:\n{feats_json}\n\n"
        f"RECENT_CANDLES (last {len(last_candles)}):\n{candles_json}\n\n"
        f"CHEATSHEET:\n{_format_chunks(chunks)}\n\n"
        "Return JSON only."
    )


_anth_client: Anthropic | None = None


def _anthropic() -> Anthropic | None:
    global _anth_client
    if not settings.ANTHROPIC_API_KEY:
        return None
    if _anth_client is None:
        _anth_client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _anth_client


def _strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


# ---------- Public entry ----------

async def analyze_polymarket(market: PolyMarket) -> PolyAnalysis:
    if settings.BYPASS_ANALYZER_BUY_FAVORITE:
        yes = market.yes_price
        no = market.no_price
        side = "YES" if yes <= no else "NO"
        entry = yes if side == "YES" else no
        return PolyAnalysis(
            score=99,
            action=f"BUY_{side}",
            confidence=0.99,
            entry_price=entry,
            reasoning=f"min-price-mode: BUY {side} @ {entry:.2f} (yes={yes:.2f}, no={no:.2f})",
            knowledge_sources=[],
            features={"mode": "min_price", "yes": yes, "no": no},
        )

    asset, interval = detect_asset_and_interval(market)
    candles: list[dict] = []
    candle_source = "none"
    if asset and interval:
        candles, candle_source = await fetch_candles(asset, interval, limit=60)
    feats = candle_features(candles)
    feats["candle_source"] = candle_source
    chunks = cheatsheet_chunks(asset, interval)
    sources = [
        f"{(c.get('metadata') or {}).get('file_name','?')} p.{(c.get('metadata') or {}).get('page','?')}"
        for c in chunks
    ]

    client = _anthropic()
    if client is not None:
        try:
            prompt = _build_user_prompt(market, feats, candles, chunks, asset, interval)
            msg = client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=500,
                system=POLY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(_strip_fence(text))
            return PolyAnalysis(
                score=int(data.get("score", 0)),
                action=str(data.get("action", "SKIP")).upper(),
                confidence=float(data.get("confidence", 0.0)),
                entry_price=float(data.get("entry_price", market.yes_price)),
                reasoning=str(data.get("reasoning", ""))[:300],
                knowledge_sources=list(data.get("knowledge_sources") or sources),
                features=feats,
                asset=asset,
                interval=interval,
            )
        except Exception as e:
            # Fall through to heuristic on Claude errors (rate limit, JSON parse, etc.)
            score, action, conf, reasoning = heuristic_decision(market, feats)
            entry = market.yes_price if action == "BUY_YES" else market.no_price
            return PolyAnalysis(
                score=score,
                action=action,
                confidence=conf,
                entry_price=entry,
                reasoning=f"claude-fallback({type(e).__name__}); {reasoning}",
                knowledge_sources=sources,
                features=feats,
                asset=asset,
                interval=interval,
            )

    score, action, conf, reasoning = heuristic_decision(market, feats)
    entry = market.yes_price if action == "BUY_YES" else market.no_price
    return PolyAnalysis(
        score=score,
        action=action,
        confidence=conf,
        entry_price=entry,
        reasoning=reasoning,
        knowledge_sources=sources,
        features=feats,
        asset=asset,
        interval=interval,
    )
