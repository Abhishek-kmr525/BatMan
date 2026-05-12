"""Live vault + auto-withdraw service for Polymarket."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import BotLog, PolyVaultEvent, PolyWallet, PolyWithdrawJob
from app.services import poly_live

_withdraw_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_live_defaults(session: AsyncSession, wallet: PolyWallet) -> PolyWallet:
    if wallet.live_trade_cap_usd is None or wallet.live_trade_cap_usd <= 0:
        wallet.live_trade_cap_usd = float(settings.POLY_LIVE_TRADE_CAP_USD)
    wallet.live_trade_balance = round(float(wallet.live_trade_balance or 0.0), 4)
    wallet.live_vault_balance = round(float(wallet.live_vault_balance or 0.0), 4)
    wallet.live_withdrawn_total = round(float(wallet.live_withdrawn_total or 0.0), 4)
    wallet.live_vault_sweeps_count = int(wallet.live_vault_sweeps_count or 0)
    return wallet


async def emit_event(
    session: AsyncSession,
    event_type: str,
    amount_usd: float,
    *,
    meta: dict | None = None,
) -> None:
    session.add(
        PolyVaultEvent(
            event_type=event_type,
            amount_usd=round(float(amount_usd or 0.0), 4),
            meta_json=meta or {},
        )
    )


async def emit_log(session: AsyncSession, message: str, level: str = "INFO", **meta) -> None:
    payload = {"platform": "polymarket", "scope": "live_vault", **(meta or {})}
    session.add(BotLog(level=level, message=message, metadata_json=payload))


async def sweep_live_excess_if_needed(
    session: AsyncSession,
    wallet: PolyWallet,
    actual_live_balance: float,
) -> tuple[PolyWallet, float]:
    wallet = await ensure_live_defaults(session, wallet)
    if not settings.POLY_LIVE_VAULT_ENABLED:
        wallet.live_trade_balance = round(max(float(actual_live_balance or 0.0), 0.0), 4)
        return wallet, 0.0

    cap = round(float(wallet.live_trade_cap_usd or settings.POLY_LIVE_TRADE_CAP_USD), 4)
    actual = round(max(float(actual_live_balance or 0.0), 0.0), 4)
    if actual > cap:
        excess = round(actual - cap, 4)
        wallet.live_trade_balance = cap
        wallet.live_vault_balance = round(float(wallet.live_vault_balance or 0.0) + excess, 4)
        wallet.live_vault_sweeps_count = int(wallet.live_vault_sweeps_count or 0) + 1
        wallet.live_last_sweep_at = _now()
        await emit_event(session, "SWEEP", excess, meta={"actual_live_balance": actual, "trade_cap_usd": cap})
        await emit_log(
            session,
            f"LIVE_VAULT_SWEEP +${excess:.2f} (live_trade->{wallet.live_trade_balance:.2f}, "
            f"live_vault->{wallet.live_vault_balance:.2f})",
        )
        return wallet, excess

    wallet.live_trade_balance = actual
    return wallet, 0.0


async def has_active_withdraw_job(session: AsyncSession) -> bool:
    q = select(func.count(PolyWithdrawJob.id)).where(
        PolyWithdrawJob.status.in_(["pending", "processing"])
    )
    count = int((await session.execute(q)).scalar_one() or 0)
    return count > 0


async def today_withdrawn_amount(session: AsyncSession) -> float:
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.coalesce(func.sum(PolyWithdrawJob.amount_usd), 0.0)).where(
        and_(
            PolyWithdrawJob.status == "succeeded",
            PolyWithdrawJob.finished_at.is_not(None),
            PolyWithdrawJob.finished_at >= start,
        )
    )
    return float((await session.execute(q)).scalar_one() or 0.0)


async def compute_auto_withdraw_eligibility(
    session: AsyncSession,
    wallet: PolyWallet,
    *,
    open_positions: int | None = None,
) -> tuple[bool, str, float]:
    await ensure_live_defaults(session, wallet)
    if not settings.POLY_LIVE_AUTO_WITHDRAW_ENABLED:
        return False, "auto_withdraw_disabled", 0.0
    if wallet.live_vault_balance < float(settings.POLY_LIVE_WITHDRAW_THRESHOLD_USD):
        return False, "below_threshold", 0.0
    if wallet.live_last_withdraw_at:
        next_time = wallet.live_last_withdraw_at + timedelta(minutes=int(settings.POLY_LIVE_WITHDRAW_COOLDOWN_MIN))
        if _now() < next_time:
            return False, "cooldown_active", 0.0
    if await has_active_withdraw_job(session):
        return False, "active_job_exists", 0.0
    if open_positions is not None and open_positions > 0:
        return False, "open_positions_nonzero", 0.0

    candidate = round(float(wallet.live_vault_balance) - float(settings.POLY_LIVE_VAULT_KEEP_BUFFER_USD), 4)
    if candidate < float(settings.POLY_LIVE_MIN_WITHDRAW_USD):
        return False, "below_min_withdraw_amount", 0.0

    already = await today_withdrawn_amount(session)
    if (already + candidate) > float(settings.POLY_LIVE_DAILY_WITHDRAW_CAP_USD):
        allowed = round(max(0.0, float(settings.POLY_LIVE_DAILY_WITHDRAW_CAP_USD) - already), 4)
        if allowed < float(settings.POLY_LIVE_MIN_WITHDRAW_USD):
            return False, "daily_cap_reached", 0.0
        return True, "eligible_partial_daily_cap", allowed
    return True, "eligible", candidate


async def request_withdraw_job(
    session: AsyncSession,
    *,
    amount_usd: float,
    requested_by: str,
) -> PolyWithdrawJob:
    amount = round(max(float(amount_usd or 0.0), 0.0), 4)
    idem = f"poly-live-{requested_by}-{uuid.uuid4().hex[:20]}"
    job = PolyWithdrawJob(
        amount_usd=amount,
        status="pending",
        idempotency_key=idem,
        requested_by=requested_by,
    )
    session.add(job)
    await emit_event(session, "WITHDRAW_REQUEST", amount, meta={"requested_by": requested_by, "idempotency_key": idem})
    await emit_log(session, f"LIVE_WITHDRAW_REQUEST ${amount:.2f} by {requested_by}")
    return job


async def execute_withdraw_job(session: AsyncSession, job: PolyWithdrawJob, wallet: PolyWallet) -> PolyWithdrawJob:
    async with _withdraw_lock:
        await ensure_live_defaults(session, wallet)
        if job.status in {"succeeded", "cancelled"}:
            return job
        if settings.POLY_LIVE_AUTO_WITHDRAW_ENABLED and hasattr(settings, "LIVE_CANARY_ENABLED"):
            # kill-switch state handled at API/bot callsite; keep lightweight guard here.
            pass
        if job.status == "processing":
            return job

        job.status = "processing"
        job.attempts = int(job.attempts or 0) + 1
        job.updated_at = _now()
        await session.flush()

        ok, tx_hash, err = await poly_live.perform_live_withdraw(job.amount_usd, job.idempotency_key)
        if ok:
            wallet.live_vault_balance = round(max(0.0, float(wallet.live_vault_balance) - float(job.amount_usd)), 4)
            wallet.live_withdrawn_total = round(float(wallet.live_withdrawn_total) + float(job.amount_usd), 4)
            wallet.live_last_withdraw_at = _now()
            job.status = "succeeded"
            job.tx_hash = tx_hash
            job.error_message = None
            job.finished_at = _now()
            await emit_event(session, "WITHDRAW_SUCCESS", job.amount_usd, meta={"tx_hash": tx_hash, "job_id": job.id})
            await emit_log(session, f"LIVE_WITHDRAW_SUCCESS ${job.amount_usd:.2f} tx={tx_hash or 'n/a'}")
            return job

        max_attempts = int(settings.POLY_LIVE_WITHDRAW_MAX_ATTEMPTS)
        job.error_message = (err or "unknown withdraw error")[:500]
        if job.attempts >= max_attempts:
            job.status = "failed"
            job.finished_at = _now()
        else:
            job.status = "pending"
        await emit_event(session, "WITHDRAW_FAIL", job.amount_usd, meta={"job_id": job.id, "attempts": job.attempts, "error": job.error_message})
        await emit_log(session, f"LIVE_WITHDRAW_FAIL ${job.amount_usd:.2f} attempt={job.attempts}: {job.error_message}", level="ERROR")
        return job
