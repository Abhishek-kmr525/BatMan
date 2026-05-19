"""Polymarket market-data helper for phase-1 paper trading."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
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
        # We pull two slices: (1) imminent up/down markets in the next 2h,
        # (2) general short-window markets ending in the next 24h.
        # Both are sorted by endDate ascending so the closest-to-resolve
        # markets land first in the candidate pool.
        try:
            now = datetime.now(timezone.utc)
            soon = now + timedelta(hours=2)
            day = now + timedelta(hours=24)
            iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

            urgent = await self._http.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "closed": "false",
                    "active": "true",
                    "limit": min(limit, 500),
                    "order": "endDate",
                    "ascending": "true",
                    "end_date_min": iso(now),
                    "end_date_max": iso(soon),
                },
            )
            urgent.raise_for_status()
            urgent_rows = urgent.json() if isinstance(urgent.json(), list) else []

            broad = await self._http.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "closed": "false",
                    "active": "true",
                    "limit": min(limit, 500),
                    "order": "endDate",
                    "ascending": "true",
                    "end_date_min": iso(now),
                    "end_date_max": iso(day),
                },
            )
            broad_rows = broad.json() if broad.status_code == 200 and isinstance(broad.json(), list) else []

            seen: set[str] = set()
            out: list[PolyMarket] = []
            for row in (urgent_rows + broad_rows):
                if not isinstance(row, dict):
                    continue
                key = str(row.get("id") or row.get("conditionId") or row.get("slug") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                m = _parse_market(row)
                if m is not None:
                    out.append(m)
                if len(out) >= limit:
                    break
            if not out:
                return _mock_markets(limit)
            return out
        except Exception:
            return _mock_markets(limit)

    async def get_wallet_recent_activity(
        self,
        wallet: str,
        *,
        limit: int = 100,
        lookback_hours: int = 24,
    ) -> dict[str, Any]:
        """Fetch recent activity for a wallet from data-api and summarize conditionIds.

        Returns:
          {
            "ok": bool,
            "count": int,
            "condition_ids": set[str],
            "latest_ts": int | None,
            "rows": list[dict]
          }
        """
        w = (wallet or "").strip().lower()
        if not w:
            return {"ok": False, "count": 0, "condition_ids": set(), "latest_ts": None, "rows": []}
        try:
            res = await self._http.get(
                "https://data-api.polymarket.com/activity",
                params={"user": w, "limit": max(1, min(int(limit), 500))},
            )
            res.raise_for_status()
            rows = res.json() if isinstance(res.json(), list) else []
            cutoff_ts = int((datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))).timestamp())
            out_rows: list[dict[str, Any]] = []
            condition_ids: set[str] = set()
            latest_ts: int | None = None
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ts = int(r.get("timestamp") or 0)
                if ts and ts < cutoff_ts:
                    continue
                cond = str(r.get("conditionId") or "").strip()
                if cond:
                    condition_ids.add(cond)
                if ts:
                    latest_ts = max(latest_ts or ts, ts)
                out_rows.append(r)
            return {
                "ok": True,
                "count": len(out_rows),
                "condition_ids": condition_ids,
                "latest_ts": latest_ts,
                "rows": out_rows,
            }
        except Exception:
            return {"ok": False, "count": 0, "condition_ids": set(), "latest_ts": None, "rows": []}


def _parse_market(row: dict[str, Any]) -> PolyMarket | None:
    title = str(row.get("question") or row.get("title") or "").strip()
    if not title:
        return None
    m_id = str(row.get("id") or row.get("conditionId") or title[:40])

    parsed_yes, parsed_no = _parse_outcome_prices(row.get("outcomePrices"))
    yes = _safe_price(row.get("yesPrice"), fallback=parsed_yes)
    no = _safe_price(row.get("noPrice"), fallback=parsed_no if parsed_no is not None else round(1 - yes, 4))
    if yes <= 0 or yes >= 1:
        yes = 0.5
    if no <= 0 or no >= 1:
        no = round(1 - yes, 4)

    volume = _safe_float(row.get("volume"), fallback=0.0)
    # Prefer endDate (full ISO timestamp). endDateIso is just the date (no time)
    # and parsing it gives midnight, which is wrong for intra-day 5m/15m markets.
    end_ts = _parse_time(row.get("endDate") or row.get("endDateIso"))
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


def _parse_outcome_prices(value: Any) -> tuple[float, float]:
    # Gamma frequently returns this as a JSON-encoded string: ["0.53","0.47"].
    if value is None:
        return 0.5, 0.5
    raw = value
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except Exception:
            return 0.5, 0.5
    if not isinstance(raw, list) or len(raw) < 2:
        return 0.5, 0.5
    yes = _safe_price(raw[0], fallback=0.5)
    no = _safe_price(raw[1], fallback=round(1 - yes, 4))
    return yes, no


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
