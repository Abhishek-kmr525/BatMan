"""Phase-4 live canary guardrails."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import PolyTrade, Trade


def _start_of_day_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


async def check_kalshi_canary(
    session: AsyncSession,
    *,
    mode: str,
    order_usd: float,
) -> tuple[bool, str, dict]:
    if not settings.LIVE_CANARY_ENABLED or mode != "live_armed":
        return True, "ok", {"canary_applied": False}
    if order_usd > settings.KALSHI_CANARY_MAX_ORDER_USD:
        return False, "canary_order_size_limit", {"order_usd": order_usd}

    start = _start_of_day_utc()
    opened_today = int(
        (
            await session.execute(
                select(func.count(Trade.id)).where(Trade.opened_at >= start)
            )
        ).scalar_one()
        or 0
    )
    if opened_today >= settings.KALSHI_CANARY_MAX_NEW_TRADES_PER_DAY:
        return False, "canary_daily_new_trade_limit", {"opened_today": opened_today}

    open_exposure = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(Trade.amount), 0.0)).where(Trade.status == "OPEN")
            )
        ).scalar_one()
        or 0.0
    )
    if open_exposure + order_usd > settings.KALSHI_CANARY_MAX_TOTAL_EXPOSURE_USD:
        return False, "canary_total_exposure_limit", {
            "open_exposure_usd": round(open_exposure, 4),
            "order_usd": order_usd,
        }
    return True, "ok", {
        "canary_applied": True,
        "opened_today": opened_today,
        "open_exposure_usd": round(open_exposure, 4),
    }


async def check_polymarket_canary(
    session: AsyncSession,
    *,
    mode: str,
    order_usd: float,
) -> tuple[bool, str, dict]:
    if not settings.LIVE_CANARY_ENABLED or mode != "live_armed":
        return True, "ok", {"canary_applied": False}
    if order_usd > settings.POLYMARKET_CANARY_MAX_ORDER_USD:
        return False, "canary_order_size_limit", {"order_usd": order_usd}

    start = _start_of_day_utc()
    opened_today = int(
        (
            await session.execute(
                select(func.count(PolyTrade.id)).where(PolyTrade.opened_at >= start)
            )
        ).scalar_one()
        or 0
    )
    if opened_today >= settings.POLYMARKET_CANARY_MAX_NEW_TRADES_PER_DAY:
        return False, "canary_daily_new_trade_limit", {"opened_today": opened_today}

    open_exposure = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(PolyTrade.amount), 0.0)).where(PolyTrade.status == "OPEN")
            )
        ).scalar_one()
        or 0.0
    )
    if open_exposure + order_usd > settings.POLYMARKET_CANARY_MAX_TOTAL_EXPOSURE_USD:
        return False, "canary_total_exposure_limit", {
            "open_exposure_usd": round(open_exposure, 4),
            "order_usd": order_usd,
        }
    return True, "ok", {
        "canary_applied": True,
        "opened_today": opened_today,
        "open_exposure_usd": round(open_exposure, 4),
    }

