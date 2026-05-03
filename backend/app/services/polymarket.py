"""Polymarket market-data helper for phase-1 paper trading."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


@dataclass
class PolyMarket:
    id: str
    title: str
    yes_price: float
    no_price: float
    volume: float
    end_ts: datetime | None
    raw: dict[str, Any]

    @property
    def close_time_seconds(self) -> int:
        if not self.end_ts:
            return 0
        return max(0, int((self.end_ts - datetime.now(timezone.utc)).total_seconds()))


class PolymarketClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def get_markets(self, limit: int = 120) -> list[PolyMarket]:
        # Public Gamma API; fallback to mock markets if unavailable.
        try:
            resp = await self._http.get(
                "https://gamma-api.polymarket.com/markets",
                params={"closed": "false", "limit": limit},
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                return _mock_markets(limit)
            out: list[PolyMarket] = []
            for row in payload[:limit]:
                out.append(_parse_market(row))
            return [m for m in out if m is not None]
        except Exception:
            return _mock_markets(limit)


def _parse_market(row: dict[str, Any]) -> PolyMarket | None:
    title = str(row.get("question") or row.get("title") or "").strip()
    if not title:
        return None
    m_id = str(row.get("id") or row.get("conditionId") or title[:40])

    yes = _safe_price(row.get("yesPrice"), fallback=0.5)
    no = _safe_price(row.get("noPrice"), fallback=round(1 - yes, 4))
    if yes <= 0 or yes >= 1:
        yes = 0.5
    if no <= 0 or no >= 1:
        no = round(1 - yes, 4)

    volume = _safe_float(row.get("volume"), fallback=0.0)
    end_ts = _parse_time(row.get("endDateIso") or row.get("endDate"))
    return PolyMarket(
        id=m_id,
        title=title,
        yes_price=yes,
        no_price=no,
        volume=volume,
        end_ts=end_ts,
        raw=row,
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _safe_price(value: Any, fallback: float) -> float:
    try:
        v = float(value)
        if v > 1.0:
            v = v / 100.0
        return round(max(0.01, min(0.99, v)), 4)
    except Exception:
        return fallback


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def _mock_markets(limit: int) -> list[PolyMarket]:
    now = datetime.now(timezone.utc)
    out = []
    for i in range(max(20, limit)):
        end_ts = now.replace(microsecond=0) + timedelta(minutes=20 + (i % 90))
        yes = round(0.1 + (i % 60) / 100, 2)
        no = round(1 - yes, 2)
        out.append(
            PolyMarket(
                id=f"poly-mock-{i}",
                title=f"Polymarket mock event #{i}",
                yes_price=yes,
                no_price=no,
                volume=float(500 + i * 25),
                end_ts=end_ts,
                raw={"mock": True},
            )
        )
    return out[:limit]


_client: PolymarketClient | None = None


def get_polymarket() -> PolymarketClient:
    global _client
    if _client is None:
        _client = PolymarketClient()
    return _client
