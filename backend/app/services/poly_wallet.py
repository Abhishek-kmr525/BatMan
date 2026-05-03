"""Paper wallet service for Polymarket phase-1."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import PolyWallet


async def ensure_wallet_initialized(session: AsyncSession) -> PolyWallet:
    res = await session.execute(select(PolyWallet).where(PolyWallet.id == 1))
    w = res.scalar_one_or_none()
    if w is None:
        w = PolyWallet(id=1, balance=settings.POLYMARKET_STARTING_BALANCE)
        session.add(w)
        await session.flush()
    return w


async def get_wallet(session: AsyncSession) -> PolyWallet:
    return await ensure_wallet_initialized(session)


async def debit(session: AsyncSession, amount: float) -> PolyWallet:
    w = await ensure_wallet_initialized(session)
    if w.balance < amount:
        raise ValueError("insufficient balance")
    w.balance = round(w.balance - amount, 4)
    return w


async def credit(session: AsyncSession, amount: float) -> PolyWallet:
    w = await ensure_wallet_initialized(session)
    w.balance = round(w.balance + amount, 4)
    return w

