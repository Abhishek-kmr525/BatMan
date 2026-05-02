"""Paper wallet service."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import Wallet


async def ensure_wallet_initialized(session: AsyncSession) -> Wallet:
    res = await session.execute(select(Wallet).where(Wallet.id == 1))
    w = res.scalar_one_or_none()
    if w is None:
        w = Wallet(id=1, balance=settings.STARTING_BALANCE)
        session.add(w)
        await session.flush()
    return w


async def get_wallet(session: AsyncSession) -> Wallet:
    return await ensure_wallet_initialized(session)


async def debit(session: AsyncSession, amount: float) -> Wallet:
    w = await ensure_wallet_initialized(session)
    if w.balance < amount:
        raise ValueError("insufficient balance")
    w.balance = round(w.balance - amount, 4)
    return w


async def credit(session: AsyncSession, amount: float) -> Wallet:
    w = await ensure_wallet_initialized(session)
    w.balance = round(w.balance + amount, 4)
    return w


async def record_close(session: AsyncSession, pnl: float, win: bool) -> Wallet:
    w = await ensure_wallet_initialized(session)
    w.total_pnl = round(w.total_pnl + pnl, 4)
    w.total_trades += 1
    if win:
        w.wins += 1
    else:
        w.losses += 1
    return w


async def deposit(session: AsyncSession, amount: float) -> Wallet:
    if amount <= 0:
        raise ValueError("amount must be > 0")
    return await credit(session, amount)
