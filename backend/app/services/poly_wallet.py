"""Paper wallet service for Polymarket phase-1."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import PolyWallet


async def ensure_wallet_initialized(session: AsyncSession) -> PolyWallet:
    res = await session.execute(select(PolyWallet).where(PolyWallet.id == 1))
    w = res.scalar_one_or_none()
    if w is None:
        w = PolyWallet(
            id=1,
            balance=settings.POLYMARKET_STARTING_BALANCE,
            trade_balance=settings.POLYMARKET_STARTING_BALANCE,
            trade_cap_usd=settings.POLYMARKET_VAULT_TRADE_CAP_USD,
        )
        session.add(w)
        await session.flush()
    # Backfill-safe defaults for pre-vault rows.
    if w.trade_balance is None:
        w.trade_balance = round(float(w.balance or 0.0), 4)
    if w.trade_cap_usd is None or w.trade_cap_usd <= 0:
        w.trade_cap_usd = float(settings.POLYMARKET_VAULT_TRADE_CAP_USD)
    if w.vault_balance is None:
        w.vault_balance = 0.0
    if w.vault_sweeps_count is None:
        w.vault_sweeps_count = 0
    _sync_legacy_balance(w)
    return w


async def get_wallet(session: AsyncSession) -> PolyWallet:
    return await ensure_wallet_initialized(session)


def _sync_legacy_balance(w: PolyWallet) -> None:
    # Keep legacy balance aligned to tradable balance for compatibility.
    w.trade_balance = round(float(w.trade_balance or 0.0), 4)
    w.vault_balance = round(float(w.vault_balance or 0.0), 4)
    w.balance = w.trade_balance


def _sweep_to_vault_if_needed(w: PolyWallet) -> float:
    if not settings.POLYMARKET_VAULT_ENABLED:
        _sync_legacy_balance(w)
        return 0.0
    cap = float(w.trade_cap_usd or settings.POLYMARKET_VAULT_TRADE_CAP_USD)
    if cap <= 0:
        cap = float(settings.POLYMARKET_VAULT_TRADE_CAP_USD)
    if w.trade_balance <= cap:
        _sync_legacy_balance(w)
        return 0.0
    excess = round(w.trade_balance - cap, 4)
    w.trade_balance = round(cap, 4)
    w.vault_balance = round((w.vault_balance or 0.0) + excess, 4)
    w.vault_sweeps_count = int(w.vault_sweeps_count or 0) + 1
    w.last_sweep_at = datetime.now(timezone.utc)
    _sync_legacy_balance(w)
    return excess


async def debit(session: AsyncSession, amount: float) -> PolyWallet:
    w = await ensure_wallet_initialized(session)
    if w.trade_balance < amount:
        raise ValueError("insufficient balance")
    w.trade_balance = round(w.trade_balance - amount, 4)
    _sync_legacy_balance(w)
    return w


async def credit(session: AsyncSession, amount: float) -> tuple[PolyWallet, float]:
    w = await ensure_wallet_initialized(session)
    w.trade_balance = round(w.trade_balance + amount, 4)
    swept = _sweep_to_vault_if_needed(w)
    return w, swept


async def enforce_vault_cap(session: AsyncSession) -> tuple[PolyWallet, float]:
    """Apply vault sweep guard outside credit paths (defensive consistency)."""
    w = await ensure_wallet_initialized(session)
    swept = _sweep_to_vault_if_needed(w)
    return w, swept


async def record_close(session: AsyncSession, pnl: float, win: bool) -> PolyWallet:
    w = await ensure_wallet_initialized(session)
    w.total_pnl = round(w.total_pnl + pnl, 4)
    w.total_trades += 1
    if win:
        w.wins += 1
    else:
        w.losses += 1
    _sync_legacy_balance(w)
    return w


async def set_trade_cap(session: AsyncSession, cap_usd: float) -> tuple[PolyWallet, float]:
    w = await ensure_wallet_initialized(session)
    w.trade_cap_usd = round(max(0.01, float(cap_usd)), 4)
    swept = _sweep_to_vault_if_needed(w)
    return w, swept


async def reset_vault(session: AsyncSession) -> PolyWallet:
    w = await ensure_wallet_initialized(session)
    w.vault_balance = 0.0
    w.vault_sweeps_count = 0
    w.last_sweep_at = None
    _sync_legacy_balance(w)
    return w


async def unlock_to_trade(session: AsyncSession, amount: float) -> PolyWallet:
    w = await ensure_wallet_initialized(session)
    move = round(max(0.0, min(float(amount), float(w.vault_balance or 0.0))), 4)
    w.vault_balance = round(float(w.vault_balance or 0.0) - move, 4)
    w.trade_balance = round(float(w.trade_balance or 0.0) + move, 4)
    _sync_legacy_balance(w)
    return w
