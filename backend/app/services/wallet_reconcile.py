"""Wallet/position consistency checks (phase-2)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import PolyTrade, Trade
from app.services import poly_wallet, wallet


async def reconcile_kalshi_paper(session: AsyncSession) -> dict:
    w = await wallet.get_wallet(session)
    open_rows = (await session.execute(select(Trade).where(Trade.status == "OPEN"))).scalars().all()
    closed_pnl = float(
        (await session.execute(select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(Trade.status.like("CLOSED%")))).scalar_one()
        or 0.0
    )
    locked = round(sum(float(t.amount or 0.0) for t in open_rows), 4)
    expected_balance = round(settings.STARTING_BALANCE + closed_pnl - locked, 4)
    actual_balance = round(float(w.balance), 4)
    delta = round(actual_balance - expected_balance, 4)
    open_entries_valid = all(0.0 < float(t.entry_price or 0) < 1.0 for t in open_rows)
    ok = actual_balance >= 0 and open_entries_valid
    return {
        "platform": "kalshi",
        "mode": "paper",
        "ok": ok,
        "actual_balance": actual_balance,
        "expected_balance": expected_balance,
        "delta": delta,
        "configured_starting_balance": settings.STARTING_BALANCE,
        "note": "delta may differ after manual reset/deposit; ok is based on structural ledger checks",
        "open_positions": len(open_rows),
        "locked_usd": locked,
        "closed_realized_pnl": round(closed_pnl, 4),
        "open_entries_valid": open_entries_valid,
    }


async def reconcile_polymarket_paper(session: AsyncSession) -> dict:
    w = await poly_wallet.get_wallet(session)
    open_rows = (await session.execute(select(PolyTrade).where(PolyTrade.status == "OPEN"))).scalars().all()
    closed_pnl = float(
        (await session.execute(select(func.coalesce(func.sum(PolyTrade.pnl), 0.0)).where(PolyTrade.status.like("CLOSED%")))).scalar_one()
        or 0.0
    )
    locked = round(sum(float(t.amount or 0.0) for t in open_rows), 4)
    expected_balance = round(settings.POLYMARKET_STARTING_BALANCE + closed_pnl - locked, 4)
    actual_balance = round(float(w.balance), 4)
    delta = round(actual_balance - expected_balance, 4)
    open_entries_valid = all(0.0 < float(t.entry_price or 0) < 1.0 for t in open_rows)
    ok = actual_balance >= 0 and open_entries_valid
    return {
        "platform": "polymarket",
        "mode": "paper",
        "ok": ok,
        "actual_balance": actual_balance,
        "expected_balance": expected_balance,
        "delta": delta,
        "configured_starting_balance": settings.POLYMARKET_STARTING_BALANCE,
        "note": "delta may differ after manual reset/deposit; ok is based on structural ledger checks",
        "open_positions": len(open_rows),
        "locked_usd": locked,
        "closed_realized_pnl": round(closed_pnl, 4),
        "open_entries_valid": open_entries_valid,
    }
