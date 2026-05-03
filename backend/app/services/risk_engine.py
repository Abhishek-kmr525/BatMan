"""Shared risk checks for Kalshi + Polymarket bots (phase-2)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.analyzer import Analysis
from app.core.config import settings
from app.models.models import PolyTrade, Trade
from app.services.kalshi import Market
from app.services.polymarket import PolyMarket


@dataclass
class RiskDecision:
    allow: bool
    reason: str
    meta: dict


def _start_of_day_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


async def _kalshi_today_realized_pnl(session: AsyncSession) -> float:
    start = _start_of_day_utc()
    q = select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
        and_(Trade.status.like("CLOSED%"), Trade.closed_at.is_not(None), Trade.closed_at >= start)
    )
    value = (await session.execute(q)).scalar_one()
    return float(value or 0.0)


async def _poly_today_realized_pnl(session: AsyncSession) -> float:
    start = _start_of_day_utc()
    q = select(func.coalesce(func.sum(PolyTrade.pnl), 0.0)).where(
        and_(PolyTrade.status.like("CLOSED%"), PolyTrade.closed_at.is_not(None), PolyTrade.closed_at >= start)
    )
    value = (await session.execute(q)).scalar_one()
    return float(value or 0.0)


async def check_kalshi_entry_risk(
    session: AsyncSession,
    market: Market,
    analysis: Analysis,
    *,
    open_positions: int,
) -> RiskDecision:
    if analysis.action not in {"BUY_YES", "BUY_NO"}:
        return RiskDecision(False, "action_not_buy", {"action": analysis.action})
    if analysis.score < settings.MIN_TRADE_SCORE:
        return RiskDecision(False, "score_below_threshold", {"score": analysis.score})
    if open_positions >= settings.MAX_CONCURRENT_POSITIONS:
        return RiskDecision(False, "position_cap_reached", {"open_positions": open_positions})
    if market.close_time_seconds <= 60:
        return RiskDecision(False, "market_too_close_to_expiry", {"ttc_seconds": market.close_time_seconds})

    day_pnl = await _kalshi_today_realized_pnl(session)
    if day_pnl <= -abs(settings.KALSHI_MAX_DAILY_LOSS_USD):
        return RiskDecision(False, "daily_loss_limit_reached", {"today_realized_pnl": day_pnl})

    return RiskDecision(True, "ok", {"today_realized_pnl": day_pnl})


async def check_polymarket_entry_risk(
    session: AsyncSession,
    market: PolyMarket,
    *,
    score: int,
    open_positions: int,
) -> RiskDecision:
    if score < settings.POLYMARKET_MIN_SCORE:
        return RiskDecision(False, "score_below_threshold", {"score": score})
    if open_positions >= settings.POLYMARKET_MAX_OPEN_POSITIONS:
        return RiskDecision(False, "position_cap_reached", {"open_positions": open_positions})
    if market.close_time_seconds < max(60, settings.POLYMARKET_MIN_TIME_TO_CLOSE_SECONDS):
        return RiskDecision(False, "market_too_close_to_expiry", {"ttc_seconds": market.close_time_seconds})
    if market.close_time_seconds > max(settings.POLYMARKET_MIN_TIME_TO_CLOSE_SECONDS, settings.POLYMARKET_MAX_TIME_TO_CLOSE_SECONDS):
        return RiskDecision(False, "market_too_far_expiry", {"ttc_seconds": market.close_time_seconds})

    day_pnl = await _poly_today_realized_pnl(session)
    if day_pnl <= -abs(settings.POLYMARKET_MAX_DAILY_LOSS_USD):
        return RiskDecision(False, "daily_loss_limit_reached", {"today_realized_pnl": day_pnl})

    return RiskDecision(True, "ok", {"today_realized_pnl": day_pnl})

