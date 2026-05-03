"""External intelligence signals for market analysis.

Integrates free/public sources:
- GDELT DOC API (global news flow)
- Guardian Open Platform (news headlines)
- FRED (macro time series; API key required)
- BLS public API (inflation/unemployment series)
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.core.config import settings
from app.services.kalshi import Market


@dataclass
class IntelSnapshot:
    query: str
    news_sentiment_score: float
    news_velocity: float
    macro_trend_score: float
    event_surprise_score: float
    data_freshness: float
    confidence_multiplier: float
    block_trade: bool
    reasons: list[str] = field(default_factory=list)
    source_health: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "news_sentiment_score": self.news_sentiment_score,
            "news_velocity": self.news_velocity,
            "macro_trend_score": self.macro_trend_score,
            "event_surprise_score": self.event_surprise_score,
            "data_freshness": self.data_freshness,
            "confidence_multiplier": self.confidence_multiplier,
            "block_trade": self.block_trade,
            "reasons": self.reasons,
            "source_health": self.source_health,
            "fetched_at": self.fetched_at,
        }


_CACHE: dict[str, tuple[float, IntelSnapshot]] = {}
_GDELT_BACKOFF_UNTIL: float = 0.0

_POSITIVE_TERMS = {
    "surge", "beats", "beat", "approved", "approval", "strong", "rise", "rises",
    "gains", "gain", "up", "upside", "bullish", "rally", "record", "growth",
    "expands", "expand", "wins", "win",
}
_NEGATIVE_TERMS = {
    "drops", "drop", "miss", "misses", "weak", "falls", "fall", "down", "bearish",
    "loss", "losses", "lawsuit", "decline", "declines", "cuts", "cut", "crash",
    "recession", "inflation", "layoffs", "ban", "halt",
}


async def gather_market_intel(market: Market) -> IntelSnapshot:
    query = _build_query(market)
    now = time.time()
    cache_key = f"{market.category}:{query}".lower()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] <= max(30, settings.INTEL_CACHE_TTL_SECONDS):
        return cached[1]

    if not settings.INTEL_FEATURES_ENABLED:
        snapshot = _neutral_snapshot(query, reason="intel disabled")
        _CACHE[cache_key] = (now, snapshot)
        return snapshot

    timeout = max(3.0, settings.INTEL_REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = []
        if settings.INTEL_USE_GDELT:
            tasks.append(_fetch_gdelt_news(client, query))
        else:
            tasks.append(asyncio.sleep(0, result={"source": "gdelt", "enabled": False}))
        if settings.INTEL_USE_GUARDIAN:
            tasks.append(_fetch_guardian_news(client, query))
        else:
            tasks.append(asyncio.sleep(0, result={"source": "guardian", "enabled": False}))
        if settings.INTEL_USE_FRED:
            tasks.append(_fetch_fred_macro(client))
        else:
            tasks.append(asyncio.sleep(0, result={"source": "fred", "enabled": False}))
        if settings.INTEL_USE_BLS:
            tasks.append(_fetch_bls_macro(client))
        else:
            tasks.append(asyncio.sleep(0, result={"source": "bls", "enabled": False}))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    news_titles: list[str] = []
    event_timestamps: list[datetime] = []
    macro_scores: list[float] = []
    reasons: list[str] = []
    source_health: dict[str, Any] = {}

    for item in results:
        if isinstance(item, Exception):
            continue
        src = str(item.get("source", "unknown"))
        source_health[src] = item

        if item.get("error"):
            reasons.append(f"{src} unavailable")
            continue

        if src in {"gdelt", "guardian"}:
            for a in item.get("articles", []):
                title = str(a.get("title", "")).strip()
                if title:
                    news_titles.append(title)
                ts = _parse_dt(a.get("published_at"))
                if ts is not None:
                    event_timestamps.append(ts)

        if src in {"fred", "bls"}:
            score = item.get("macro_score")
            if isinstance(score, (int, float)):
                macro_scores.append(float(score))
            ts = _parse_dt(item.get("published_at"))
            if ts is not None:
                event_timestamps.append(ts)

    sentiment = _headline_sentiment(news_titles)
    velocity = _clamp(len(news_titles) / 20.0, 0.0, 1.0)
    macro = _clamp(sum(macro_scores) / len(macro_scores), -1.0, 1.0) if macro_scores else 0.0
    freshness = _freshness_score(event_timestamps)

    surprise_mag = _clamp(abs(sentiment) * 0.55 + velocity * 0.45, 0.0, 1.0)
    surprise = round(math.copysign(surprise_mag, sentiment if sentiment != 0 else 1), 4)

    confidence_multiplier = 1.0
    if freshness < settings.INTEL_MIN_FRESHNESS:
        confidence_multiplier *= 0.72
        reasons.append("stale signal window")
    if len(news_titles) < settings.INTEL_MIN_NEWS_ITEMS:
        confidence_multiplier *= 0.82
        reasons.append("low news coverage")
    if abs(macro) >= 0.8:
        confidence_multiplier *= 0.9
        reasons.append("macro volatility elevated")
    confidence_multiplier = _clamp(confidence_multiplier, 0.45, 1.05)

    block_trade = bool(
        settings.INTEL_STRICT_SKIP
        and freshness < settings.INTEL_MIN_FRESHNESS
        and len(news_titles) < settings.INTEL_MIN_NEWS_ITEMS
    )

    snapshot = IntelSnapshot(
        query=query,
        news_sentiment_score=round(sentiment, 4),
        news_velocity=round(velocity, 4),
        macro_trend_score=round(macro, 4),
        event_surprise_score=surprise,
        data_freshness=round(freshness, 4),
        confidence_multiplier=round(confidence_multiplier, 4),
        block_trade=block_trade,
        reasons=reasons,
        source_health=source_health,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    _CACHE[cache_key] = (now, snapshot)
    return snapshot


def _neutral_snapshot(query: str, reason: str) -> IntelSnapshot:
    return IntelSnapshot(
        query=query,
        news_sentiment_score=0.0,
        news_velocity=0.0,
        macro_trend_score=0.0,
        event_surprise_score=0.0,
        data_freshness=0.0,
        confidence_multiplier=0.9,
        block_trade=False,
        reasons=[reason],
        source_health={},
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _build_query(market: Market) -> str:
    title = (market.title or "").replace("?", " ")
    words = [w.lower() for w in title.split() if len(w) >= 3]
    keywords = words[:6]
    cat = (market.category or "").lower()

    if "crypto" in cat or "btc" in market.ticker.lower() or "eth" in market.ticker.lower():
        keywords.extend(["bitcoin", "crypto"])
    if "mlb" in market.ticker.lower() or "nba" in market.ticker.lower() or "nfl" in market.ticker.lower():
        keywords.extend(["sports"])
    if any(x in market.ticker.lower() for x in ("cpi", "fed", "jobs", "gdp", "unemp")):
        keywords.extend(["economy", "inflation", "rates"])

    dedup: list[str] = []
    for w in keywords:
        if w not in dedup:
            dedup.append(w)
    if not dedup:
        dedup = ["kalshi", "market"]
    return " ".join(dedup[:8])


async def _fetch_gdelt_news(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    global _GDELT_BACKOFF_UNTIL
    now = time.time()
    if now < _GDELT_BACKOFF_UNTIL:
        return {"source": "gdelt", "error": "rate_limited_backoff", "articles": []}

    q = quote_plus(query)
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={q}&mode=ArtList&maxrecords=15&format=json&timespan=24h"
    )
    try:
        resp = await client.get(url)
        if resp.status_code == 429:
            _GDELT_BACKOFF_UNTIL = now + max(6, settings.INTEL_GDELT_MIN_INTERVAL_SECONDS)
            return {"source": "gdelt", "error": "rate_limited", "articles": []}
        resp.raise_for_status()
        payload = resp.json()
        raw_articles = payload.get("articles", []) or []
        articles = []
        for a in raw_articles[:15]:
            articles.append(
                {
                    "title": str(a.get("title", ""))[:220],
                    "published_at": a.get("seendate"),
                }
            )
        return {"source": "gdelt", "articles": articles, "count": len(articles)}
    except Exception as exc:
        return {"source": "gdelt", "error": f"{type(exc).__name__}", "articles": []}


async def _fetch_guardian_news(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    api_key = settings.GUARDIAN_API_KEY or "test"
    try:
        resp = await client.get(
            "https://content.guardianapis.com/search",
            params={
                "q": query,
                "page-size": 15,
                "order-by": "newest",
                "show-fields": "headline",
                "api-key": api_key,
            },
        )
        resp.raise_for_status()
        payload = resp.json().get("response", {})
        results = payload.get("results", []) or []
        articles = [
            {
                "title": str(r.get("webTitle", ""))[:220],
                "published_at": r.get("webPublicationDate"),
            }
            for r in results[:15]
        ]
        return {"source": "guardian", "articles": articles, "count": len(articles)}
    except Exception as exc:
        return {"source": "guardian", "error": f"{type(exc).__name__}", "articles": []}


async def _fetch_fred_macro(client: httpx.AsyncClient) -> dict[str, Any]:
    if not settings.FRED_API_KEY:
        return {"source": "fred", "error": "missing_api_key"}

    try:
        series_ids = ["CPIAUCSL", "UNRATE"]
        scores: list[float] = []
        latest_ts: datetime | None = None
        for sid in series_ids:
            resp = await client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": sid,
                    "api_key": settings.FRED_API_KEY,
                    "file_type": "json",
                    "limit": 3,
                    "sort_order": "desc",
                },
            )
            resp.raise_for_status()
            obs = (resp.json().get("observations") or [])[:2]
            if len(obs) < 2:
                continue
            v0 = _safe_float(obs[0].get("value"))
            v1 = _safe_float(obs[1].get("value"))
            if v0 is None or v1 is None or abs(v1) < 1e-9:
                continue
            pct = (v0 - v1) / abs(v1)
            if sid == "CPIAUCSL":
                score = -_clamp(pct * 12.0, -1.0, 1.0)
            else:
                score = -_clamp(pct * 20.0, -1.0, 1.0)
            scores.append(score)
            dt = _parse_dt(obs[0].get("date"))
            if dt and (latest_ts is None or dt > latest_ts):
                latest_ts = dt

        macro = _clamp(sum(scores) / len(scores), -1.0, 1.0) if scores else 0.0
        return {
            "source": "fred",
            "macro_score": macro,
            "published_at": latest_ts.isoformat() if latest_ts else None,
            "series_count": len(scores),
        }
    except Exception as exc:
        return {"source": "fred", "error": f"{type(exc).__name__}"}


async def _fetch_bls_macro(client: httpx.AsyncClient) -> dict[str, Any]:
    try:
        now = datetime.now(timezone.utc)
        start_year = str(now.year - 1)
        end_year = str(now.year)
        resp = await client.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json={
                "seriesid": ["CUUR0000SA0", "LNS14000000"],
                "startyear": start_year,
                "endyear": end_year,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        series = ((payload.get("Results") or {}).get("series") or [])
        scores: list[float] = []
        latest_ts: datetime | None = None
        for s in series:
            sid = s.get("seriesID")
            rows = [r for r in (s.get("data") or []) if str(r.get("period", "")).startswith("M")]
            if len(rows) < 2:
                continue
            v0 = _safe_float(rows[0].get("value"))
            v1 = _safe_float(rows[1].get("value"))
            if v0 is None or v1 is None or abs(v1) < 1e-9:
                continue
            pct = (v0 - v1) / abs(v1)
            if sid == "CUUR0000SA0":
                score = -_clamp(pct * 10.0, -1.0, 1.0)
            else:
                score = -_clamp(pct * 12.0, -1.0, 1.0)
            scores.append(score)

            dt = _parse_bls_month(rows[0].get("year"), rows[0].get("period"))
            if dt and (latest_ts is None or dt > latest_ts):
                latest_ts = dt

        macro = _clamp(sum(scores) / len(scores), -1.0, 1.0) if scores else 0.0
        return {
            "source": "bls",
            "macro_score": macro,
            "published_at": latest_ts.isoformat() if latest_ts else None,
            "series_count": len(scores),
        }
    except Exception as exc:
        return {"source": "bls", "error": f"{type(exc).__name__}"}


def _headline_sentiment(titles: list[str]) -> float:
    if not titles:
        return 0.0
    pos = 0
    neg = 0
    for t in titles:
        tokens = [w.lower() for w in t.split()]
        token_set = {w.strip(".,:;!?()[]{}'\"") for w in tokens}
        pos += sum(1 for w in token_set if w in _POSITIVE_TERMS)
        neg += sum(1 for w in token_set if w in _NEGATIVE_TERMS)
    total = pos + neg
    if total == 0:
        return 0.0
    return _clamp((pos - neg) / total, -1.0, 1.0)


def _freshness_score(timestamps: list[datetime]) -> float:
    if not timestamps:
        return 0.0
    latest = max(timestamps)
    age_hours = max(0.0, (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0)
    if age_hours <= 1:
        return 1.0
    if age_hours <= 6:
        return _clamp(1.0 - (age_hours - 1) * 0.1, 0.0, 1.0)
    if age_hours <= 24:
        return _clamp(0.5 - (age_hours - 6) * 0.02, 0.0, 1.0)
    return 0.0


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
        if len(text) == 14 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_bls_month(year: Any, period: Any) -> datetime | None:
    try:
        y = int(str(year))
        p = str(period)
        if not p.startswith("M"):
            return None
        m = int(p[1:])
        if m < 1 or m > 12:
            return None
        return datetime(y, m, 1, tzinfo=timezone.utc)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if text in {".", ""}:
            return None
        return float(text)
    except Exception:
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
