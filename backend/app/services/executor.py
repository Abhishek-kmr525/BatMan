"""Trade Executor — places $1 buys, monitors positions, executes sells."""
from __future__ import annotations

from enum import StrEnum
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.analyzer import Analysis
from app.core.config import settings
from app.models.models import Trade
from app.services import wallet
from app.services.kalshi import KalshiClient, Market


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OpenTradeRejectReason(StrEnum):
    ACTION_NOT_BUY = "action_not_buy"
    SCORE_BELOW_THRESHOLD = "score_below_threshold"
    DUPLICATE_MARKET_POSITION = "duplicate_market_position"
    DUPLICATE_EVENT_POSITION = "duplicate_event_position"
    POSITION_CAP_REACHED = "position_cap_reached"
    INSUFFICIENT_BALANCE = "insufficient_balance"


async def open_count(session: AsyncSession) -> int:
    res = await session.execute(
        select(func.count(Trade.id)).where(Trade.status == "OPEN")
    )
    return int(res.scalar() or 0)


async def has_open_for_market(session: AsyncSession, market_id: str) -> bool:
    res = await session.execute(
        select(Trade.id).where(Trade.market_id == market_id, Trade.status == "OPEN")
    )
    return res.first() is not None


async def has_open_for_event(session: AsyncSession, market: Market) -> bool:
    """Avoid taking multiple correlated positions on the same event question."""
    res = await session.execute(
        select(Trade.id).where(
            Trade.market_title == market.title,
            Trade.category == market.category,
            Trade.status == "OPEN",
        )
    )
    return res.first() is not None


async def open_trade(
    session: AsyncSession,
    kalshi: KalshiClient,
    market: Market,
    analysis: Analysis,
    *,
    min_score: int | None = None,
    strategy_id: str | None = None,
    source: str = "bot",
) -> Trade | None:
    trade, _ = await open_trade_with_reason(
        session,
        kalshi,
        market,
        analysis,
        min_score=min_score,
        strategy_id=strategy_id,
        source=source,
    )
    return trade


async def open_trade_with_reason(
    session: AsyncSession,
    kalshi: KalshiClient,
    market: Market,
    analysis: Analysis,
    *,
    min_score: int | None = None,
    strategy_id: str | None = None,
    source: str = "bot",
) -> tuple[Trade | None, OpenTradeRejectReason | None]:
    if analysis.action not in ("BUY_YES", "BUY_NO"):
        return None, OpenTradeRejectReason.ACTION_NOT_BUY
    threshold = settings.MIN_TRADE_SCORE if min_score is None else min_score
    if analysis.score < threshold:
        return None, OpenTradeRejectReason.SCORE_BELOW_THRESHOLD
    if await has_open_for_market(session, market.ticker):
        return None, OpenTradeRejectReason.DUPLICATE_MARKET_POSITION
    if await has_open_for_event(session, market):
        return None, OpenTradeRejectReason.DUPLICATE_EVENT_POSITION
    if await open_count(session) >= settings.MAX_CONCURRENT_POSITIONS:
        return None, OpenTradeRejectReason.POSITION_CAP_REACHED

    direction = "YES" if analysis.action == "BUY_YES" else "NO"
    side = direction.lower()
    entry = market.yes_price if direction == "YES" else market.no_price

    try:
        await wallet.debit(session, settings.TRADE_AMOUNT_USD)
    except ValueError:
        return None, OpenTradeRejectReason.INSUFFICIENT_BALANCE

    await kalshi.place_order(
        ticker=market.ticker,
        action="buy",
        side=side,
        count=1,
        price_cents=int(round(entry * 100)),
    )

    trade = Trade(
        market_id=market.ticker,
        market_title=market.title,
        category=market.category,
        direction=direction,
        amount=settings.TRADE_AMOUNT_USD,
        entry_price=entry,
        target_exit_price=analysis.target_exit_price,
        stop_loss_price=analysis.stop_loss_price,
        agent_score=analysis.score,
        reasoning=analysis.reasoning,
        status="OPEN",
        strategy_id=strategy_id,
        source=source,
    )
    session.add(trade)
    await session.flush()
    return trade, None


async def list_open(session: AsyncSession) -> list[Trade]:
    res = await session.execute(select(Trade).where(Trade.status == "OPEN"))
    return list(res.scalars().all())


async def evaluate_exit(
    session: AsyncSession, kalshi: KalshiClient, trade: Trade
) -> Trade | None:
    """Decide if trade should close. Returns the (now-closed) trade or None."""
    market = await kalshi.get_market(trade.market_id)
    if market is None:
        return None
    side = trade.direction
    cur = market.yes_price if side == "YES" else market.no_price

    reason = None
    if cur >= (trade.target_exit_price or 1.0):
        reason = "TAKE_PROFIT"
    elif cur <= (trade.stop_loss_price or 0.0):
        reason = "STOP_LOSS"
    elif market.close_time_seconds <= 5 * 60:
        reason = "TIME_EXIT"

    if reason is None:
        return None

    return await close_trade(session, kalshi, trade, exit_price=cur, exit_reason=reason)


async def close_trade(
    session: AsyncSession,
    kalshi: KalshiClient,
    trade: Trade,
    exit_price: float,
    exit_reason: str,
) -> Trade:
    side = trade.direction.lower()
    await kalshi.place_order(
        ticker=trade.market_id, action="sell", side=side, count=1
    )

    # PnL: $1 buys 1/entry contracts; payoff at exit = contracts * exit_price.
    contracts = trade.amount / max(trade.entry_price, 0.01)
    payout = contracts * exit_price
    pnl = round(payout - trade.amount, 4)

    trade.exit_price = exit_price
    trade.pnl = pnl
    trade.status = f"CLOSED_{exit_reason}"
    trade.closed_at = _now()

    await wallet.credit(session, payout)
    await wallet.record_close(session, pnl=pnl, win=pnl > 0)
    return trade
