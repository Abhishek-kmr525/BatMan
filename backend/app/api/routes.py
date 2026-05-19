from __future__ import annotations

import asyncio
import time
import csv
import io
import os
import shutil
import re
import uuid
from datetime import datetime, timedelta, timezone
import httpx

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import knowledge
from app.agent.analyzer import analyze_market, check_claude_health, check_gemini_health
from app.core.config import settings
from app.core.db import get_session
from app.models.models import BotLog, CandleTrade, CandleWallet, PolyTrade, PolyVaultEvent, PolyWithdrawJob, Trade
from app.services import executor as exec_svc
from app.services import poly_wallet, strategies, wallet
from app.services.bot import bot
from app.services.bot_polymarket import poly_bot, poly_live_bot, poly_paper_bot
from app.services.bot_candle import candle_live_bot, candle_paper_bot
from app.services import binance_data, binance_live, candle_strategy as candle_strat
from app.services.events import bus
from app.services.intel import gather_market_intel
from app.services.kalshi import get_kalshi
from app.services.kalshi import Market
from app.services.mode_guard import mode_guard
from app.services.poly_live import get_live_balance, get_live_balance_error, get_live_preflight
from app.services.poly_live_vault import (
    compute_auto_withdraw_eligibility,
    execute_withdraw_job,
    has_active_withdraw_job,
    request_withdraw_job,
    sweep_live_excess_if_needed,
)
from app.services.polymarket import get_polymarket
from app.services.wallet_reconcile import reconcile_kalshi_paper, reconcile_polymarket_paper
from app.services.canary_guard import check_kalshi_canary, check_polymarket_canary

router = APIRouter()
_MARKET_CLOSE_CACHE: dict[str, tuple[float, int | None]] = {}
_MARKET_CLOSE_CACHE_TTL_SECONDS = 30.0
_POLY_LIVE_AUTO_WITHDRAW_OVERRIDE: bool | None = None


def _poly_live_auto_withdraw_enabled() -> bool:
    if _POLY_LIVE_AUTO_WITHDRAW_OVERRIDE is None:
        return bool(settings.POLY_LIVE_AUTO_WITHDRAW_ENABLED)
    return bool(_POLY_LIVE_AUTO_WITHDRAW_OVERRIDE)


class TradingViewWebhookBody(BaseModel):
    symbol: str = ""
    timeframe: str = ""
    time: int | None = None
    close: float | None = None
    decision: str | None = None
    confidence: float | int | None = None
    tp: float | None = None
    sl: float | None = None
    signal_id: str | None = None
    strategy: str | None = None
    meta: dict | None = None
    action: str | None = None
    qty: float | None = None
    mode: str | None = None


class TradingViewTestOrderBody(BaseModel):
    action: str  # SELL to open short, BUY to close short
    symbol: str = "BTCUSD"
    close: float | None = None
    mode: str = "paper"
    timeframe: str = "1"
    confidence: float | int | None = 70
    tp: float | None = None
    sl: float | None = None


def _tv_normalize_symbol(raw: str) -> str:
    s = (raw or "").upper().strip()
    if not s:
        return "BTCUSDT"
    if ":" in s:
        s = s.split(":")[-1]
    if s.endswith("PERP"):
        s = s[:-4]
    if s.endswith("USD") and not s.endswith("USDT"):
        s = s + "T"
    if s in {"BTC", "ETH", "SOL", "BNB", "XRP"}:
        return s + "USDT"
    return s


def _tv_parse_text_message(msg: str) -> dict:
    text = (msg or "").strip()
    action = None
    if re.search(r"\border\s+sell\b", text, flags=re.IGNORECASE):
        action = "SELL"
    elif re.search(r"\border\s+buy\b", text, flags=re.IGNORECASE):
        action = "BUY"

    qty = None
    m_qty = re.search(r"@\s*([-+]?\d*\.?\d+)", text)
    if m_qty:
        try:
            qty = float(m_qty.group(1))
        except Exception:
            qty = None

    symbol = ""
    m_sym = re.search(r"on\s+([A-Z0-9:\-_.]+)", text, flags=re.IGNORECASE)
    if m_sym:
        symbol = m_sym.group(1).strip().upper()

    return {"action": action, "qty": qty, "symbol": symbol}


async def _candle_ensure_wallet(session: AsyncSession) -> CandleWallet:
    w = (await session.execute(select(CandleWallet).where(CandleWallet.id == 1))).scalar_one_or_none()
    if w is None:
        w = CandleWallet(
            id=1,
            paper_balance=settings.CANDLE_PAPER_STARTING_BALANCE,
            paper_starting_balance=settings.CANDLE_PAPER_STARTING_BALANCE,
        )
        session.add(w)
        await session.flush()
    return w


async def _tradingview_openai_decide(payload: TradingViewWebhookBody) -> dict:
    """Ask OpenAI for BUY/NO_BUY + TP/SL reasoning from TradingView snapshot."""
    if not settings.OPENAI_API_KEY:
        return {
            "decision": "NO_BUY",
            "confidence": 0,
            "tp": payload.tp,
            "sl": payload.sl,
            "reason": "OPENAI_API_KEY missing; configure backend .env",
            "model": None,
            "provider": "fallback",
        }

    system_prompt = (
        "You are a strict intraday trading advisor for 1-minute candles. "
        "Return only a compact JSON object with keys: decision, confidence, tp, sl, reason. "
        "decision must be BUY or NO_BUY. confidence must be 0-100 integer. "
        "tp and sl must be numbers (or null). Keep reason under 160 chars."
    )
    user_payload = {
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "time": payload.time,
        "close": payload.close,
        "tv_decision": payload.decision,
        "tv_confidence": payload.confidence,
        "tv_tp": payload.tp,
        "tv_sl": payload.sl,
        "strategy": payload.strategy,
        "meta": payload.meta or {},
    }
    req_json = {
        "model": settings.TRADINGVIEW_OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"TradingView snapshot: {user_payload}"},
        ],
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            r = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=req_json,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {
            "decision": "NO_BUY",
            "confidence": 0,
            "tp": payload.tp,
            "sl": payload.sl,
            "reason": f"openai call failed: {str(e)[:120]}",
            "model": settings.TRADINGVIEW_OPENAI_MODEL,
            "provider": "fallback",
        }

    output_text = str(data.get("output_text") or "").strip()
    parsed: dict = {}
    if output_text.startswith("{") and output_text.endswith("}"):
        try:
            import json
            parsed = json.loads(output_text)
        except Exception:
            parsed = {}
    decision = str(parsed.get("decision") or "").upper()
    if decision not in {"BUY", "NO_BUY"}:
        decision = "NO_BUY"
    confidence = int(parsed.get("confidence") or 0)
    confidence = max(0, min(100, confidence))
    tp = parsed.get("tp", payload.tp)
    sl = parsed.get("sl", payload.sl)
    reason = str(parsed.get("reason") or "no reason").strip()[:160]
    return {
        "decision": decision,
        "confidence": confidence,
        "tp": tp,
        "sl": sl,
        "reason": reason,
        "model": settings.TRADINGVIEW_OPENAI_MODEL,
        "provider": "openai",
        "raw_text": output_text[:500],
    }


# ---------- Bot ----------
@router.post("/bot/start")
async def bot_start():
    await bot.start()
    return bot.status()


@router.post("/bot/stop")
async def bot_stop():
    await bot.stop()
    return bot.status()


@router.get("/bot/status")
async def bot_status(session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(Trade).where(Trade.status == "OPEN")
    )
    active = len(list(res.scalars().all()))
    s = bot.status()
    s["active_positions"] = active
    s["max_concurrent_positions"] = settings.MAX_CONCURRENT_POSITIONS
    s["mode_guard"] = mode_guard.get("kalshi").to_dict()
    return s


# ---------- Wallet ----------
@router.get("/wallet")
async def wallet_get(session: AsyncSession = Depends(get_session)):
    w = await wallet.get_wallet(session)
    win_rate = (w.wins / w.total_trades * 100) if w.total_trades else 0.0
    # best/worst trade
    best = (await session.execute(
        select(Trade).where(Trade.status.like("CLOSED%")).order_by(desc(Trade.pnl)).limit(1)
    )).scalar_one_or_none()
    worst = (await session.execute(
        select(Trade).where(Trade.status.like("CLOSED%")).order_by(Trade.pnl).limit(1)
    )).scalar_one_or_none()
    return {
        "balance": round(w.balance, 4),
        "total_pnl": round(w.total_pnl, 4),
        "total_trades": w.total_trades,
        "wins": w.wins,
        "losses": w.losses,
        "win_rate": round(win_rate, 2),
        "best_trade": _trade_dict(best) if best else None,
        "worst_trade": _trade_dict(worst) if worst else None,
    }


class DepositBody(BaseModel):
    amount: float


@router.post("/wallet/deposit")
async def wallet_deposit(body: DepositBody, session: AsyncSession = Depends(get_session)):
    w = await wallet.deposit(session, body.amount)
    await session.commit()
    await bus.publish("wallet:updated", {"balance": w.balance})
    return {"new_balance": w.balance}


class DemoResetBody(BaseModel):
    passcode: str
    balance: float = 20.0


@router.post("/maintenance/demo/reset")
async def maintenance_demo_reset(body: DemoResetBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    await bot.stop()
    await session.execute(delete(Trade))
    w = await wallet.get_wallet(session)
    w.balance = round(max(body.balance, 0.0), 4)
    w.total_pnl = 0.0
    w.total_trades = 0
    w.wins = 0
    w.losses = 0
    await session.commit()
    await bus.publish("wallet:updated", {"balance": w.balance})
    await bus.publish("trades:cleared", {"ok": True})
    return {"ok": True, "balance": w.balance, "trades_deleted": "all"}


class PolyResetBody(BaseModel):
    passcode: str
    balance: float = 20.0


class MaintenanceCleanupBody(BaseModel):
    passcode: str
    keep_recent_logs: int = 1000


class MaintenanceReclaimBody(BaseModel):
    passcode: str


class MaintenanceInspectBody(BaseModel):
    passcode: str
    top_n: int = 30


@router.post("/maintenance/poly/reset")
async def maintenance_poly_reset(body: PolyResetBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    await poly_bot.stop()
    await session.execute(delete(PolyTrade))
    w = await poly_wallet.get_wallet(session)
    w.balance = round(max(body.balance, 0.0), 4)
    w.trade_balance = w.balance
    w.vault_balance = 0.0
    w.trade_cap_usd = float(settings.POLYMARKET_VAULT_TRADE_CAP_USD)
    w.vault_sweeps_count = 0
    w.last_sweep_at = None
    w.total_pnl = 0.0
    w.total_trades = 0
    w.wins = 0
    w.losses = 0
    await session.commit()
    await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
    await bus.publish("polymarket:trades:cleared", {"ok": True})
    return {"ok": True, "balance": w.balance, "trades_deleted": "all"}


@router.post("/maintenance/storage/cleanup")
async def maintenance_storage_cleanup(
    body: MaintenanceCleanupBody,
    session: AsyncSession = Depends(get_session),
):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")

    # SQLite in production can hit "too many SQL variables" with large IN lists.
    # For emergency storage recovery, purge log table in one statement.
    total_before = int((await session.execute(select(func.count(BotLog.id)))).scalar_one() or 0)
    await session.execute(delete(BotLog))
    deleted_logs = total_before

    await session.commit()
    # Best-effort SQLite reclaim for /data disk pressure.
    try:
        await session.execute(text("VACUUM"))
        await session.commit()
    except Exception:
        await session.rollback()

    await bus.publish("maintenance:storage:cleanup", {"deleted_logs": deleted_logs, "keep_recent_logs": body.keep_recent_logs})
    return {"ok": True, "deleted_logs": deleted_logs, "keep_recent_logs": body.keep_recent_logs}


class MaintenanceDeleteFileBody(BaseModel):
    passcode: str
    paths: list[str]


@router.post("/maintenance/storage/delete-files")
async def maintenance_delete_files(body: MaintenanceDeleteFileBody):
    """Delete specific files to free disk space. Use to reclaim large PDFs/caches."""
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    removed: list[dict] = []
    failed: list[dict] = []
    for p in body.paths:
        try:
            if os.path.isfile(p):
                size = os.path.getsize(p)
                os.remove(p)
                removed.append({"path": p, "freed_bytes": size})
            else:
                failed.append({"path": p, "error": "not_a_file_or_missing"})
        except Exception as e:
            failed.append({"path": p, "error": str(e)[:200]})
    return {"ok": len(failed) == 0, "removed": removed, "failed": failed}


@router.post("/maintenance/storage/emergency-purge")
async def maintenance_emergency_purge(body: MaintenanceReclaimBody):
    """Disk-full emergency: use a raw aiosqlite connection bypassing SQLAlchemy
    transaction overhead, set temp_store + journal to MEMORY so no disk write
    is needed, then DROP + recreate bot_logs and VACUUM into RAM."""
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    import aiosqlite
    db_path = "/data/amta.db"
    actions: list[str] = []
    try:
        size_before = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        actions.append(f"db_size_before={size_before/1024/1024:.2f}MB")
        async with aiosqlite.connect(db_path) as conn:
            # Configure SQLite to avoid all disk writes for journal/temp.
            for pragma in [
                "PRAGMA temp_store=MEMORY",
                "PRAGMA cache_size=-200000",  # ~200MB cache
                "PRAGMA journal_mode=MEMORY",
                "PRAGMA synchronous=OFF",
                "PRAGMA locking_mode=EXCLUSIVE",
            ]:
                try:
                    await conn.execute(pragma)
                    actions.append(pragma)
                except Exception as e:
                    actions.append(f"{pragma}_failed: {str(e)[:80]}")
            # Drop heavy bot_logs.
            try:
                await conn.execute("DROP TABLE IF EXISTS bot_logs")
                await conn.commit()
                actions.append("dropped bot_logs")
            except Exception as e:
                actions.append(f"drop_failed: {str(e)[:120]}")
            # Recreate empty bot_logs.
            try:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS bot_logs (
                        id VARCHAR PRIMARY KEY,
                        level VARCHAR(10) DEFAULT 'INFO',
                        message TEXT NOT NULL,
                        metadata_json JSON,
                        created_at DATETIME NOT NULL
                    )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS ix_bot_logs_created_at ON bot_logs(created_at)")
                await conn.commit()
                actions.append("recreated bot_logs")
            except Exception as e:
                actions.append(f"recreate_failed: {str(e)[:120]}")
            # Restore WAL journaling.
            try:
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                actions.append("restored WAL")
            except Exception:
                pass
            # VACUUM with MEMORY temp_store — needs RAM equal to DB size.
            try:
                await conn.execute("VACUUM")
                actions.append("VACUUM ok")
            except Exception as e:
                actions.append(f"vacuum_failed: {str(e)[:120]}")
        size_after = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        actions.append(f"db_size_after={size_after/1024/1024:.2f}MB")
        actions.append(f"freed={(size_before - size_after)/1024/1024:.2f}MB")
        return {"ok": True, "actions": actions}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "actions": actions}


@router.post("/maintenance/storage/reclaim")
async def maintenance_storage_reclaim(body: MaintenanceReclaimBody):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")

    targets = [
        "/data/chroma",
        "/data/chroma_live",
        "/data/knowledge_index",
        "./data/chroma",
        "./knowledge/chroma",
    ]
    removed: list[str] = []
    missing: list[str] = []
    failed: list[dict[str, str]] = []

    for p in targets:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=False)
                removed.append(p)
            else:
                missing.append(p)
        except Exception as e:
            failed.append({"path": p, "error": str(e)})

    await bus.publish("maintenance:storage:reclaim", {"removed": removed, "failed": failed})
    return {"ok": len(failed) == 0, "removed": removed, "missing": missing, "failed": failed}


@router.post("/maintenance/storage/inspect")
async def maintenance_storage_inspect(body: MaintenanceInspectBody):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")

    roots = ["/data", "./data", "./knowledge"]
    top_n = max(1, min(int(body.top_n), 200))
    files: list[dict[str, float | str]] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, _dirs, names in os.walk(root):
            for nm in names:
                p = os.path.join(base, nm)
                try:
                    sz = float(os.path.getsize(p))
                except Exception:
                    continue
                files.append({"path": p, "size_mb": round(sz / (1024 * 1024), 4)})

    files.sort(key=lambda x: float(x.get("size_mb", 0.0)), reverse=True)
    return {"ok": True, "roots": roots, "files": files[:top_n]}


# ---------- Trades ----------
@router.get("/trades")
async def trades_list(
    status: str = "open",
    limit: int = 50,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
):
    q = select(Trade)
    if status == "open":
        q = q.where(Trade.status == "OPEN")
    elif status == "closed":
        q = q.where(Trade.status.like("CLOSED%"))
    q = q.order_by(desc(Trade.opened_at)).offset(max(page - 1, 0) * limit).limit(limit)
    res = await session.execute(q)
    rows = list(res.scalars().all())

    # Enrich open positions with current price + close countdown (best-effort)
    if rows:
        kalshi = get_kalshi()
        out = []
        for t in rows:
            cur = None
            ttc: int | None = None
            try:
                if t.status == "OPEN":
                    cur = await kalshi.current_price(t.market_id, t.direction)
                    ttc = await _time_to_close_seconds(kalshi, t.market_id)
            except Exception:
                pass
            d = _trade_dict(t)
            d["current_price"] = cur
            d["time_to_close_seconds"] = ttc
            if cur is not None:
                contracts = t.amount / max(t.entry_price, 0.01)
                d["unrealized_pnl"] = round(contracts * cur - t.amount, 4)
            out.append(d)
        return out
    return [_trade_dict(t) for t in rows]


@router.get("/trades/summary")
async def trades_summary(session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    start_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    total_q = select(func.count(Trade.id))
    open_q = select(func.count(Trade.id)).where(Trade.status == "OPEN")
    closed_q = select(func.count(Trade.id)).where(Trade.status.like("CLOSED%"))
    today_open_q = select(func.count(Trade.id)).where(Trade.opened_at >= start_utc)
    today_closed_q = select(func.count(Trade.id)).where(
        Trade.closed_at.is_not(None), Trade.closed_at >= start_utc
    )

    total = int((await session.execute(total_q)).scalar_one() or 0)
    open_count = int((await session.execute(open_q)).scalar_one() or 0)
    closed_count = int((await session.execute(closed_q)).scalar_one() or 0)
    today_opened = int((await session.execute(today_open_q)).scalar_one() or 0)
    today_closed = int((await session.execute(today_closed_q)).scalar_one() or 0)

    return {
        "total_count": total,
        "open_count": open_count,
        "closed_count": closed_count,
        "today_opened_count": today_opened,
        "today_closed_count": today_closed,
    }


@router.get("/trades/{trade_id}")
async def trade_get(trade_id: str, session: AsyncSession = Depends(get_session)):
    res = await session.execute(select(Trade).where(Trade.id == trade_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="trade not found")
    return _trade_dict(t)


# ---------- Polymarket ----------
@router.post("/polymarket/bot/start")
async def polymarket_bot_start():
    await poly_paper_bot.start()
    return poly_paper_bot.status()


@router.post("/polymarket/bot/stop")
async def polymarket_bot_stop():
    await poly_paper_bot.stop()
    return poly_paper_bot.status()


@router.get("/polymarket/bot/status")
async def polymarket_bot_status(session: AsyncSession = Depends(get_session)):
    current_mode = "paper"
    res = await session.execute(
        select(PolyTrade).where(PolyTrade.status == "OPEN", PolyTrade.mode == current_mode)
    )
    active = len(list(res.scalars().all()))
    s = poly_paper_bot.status()
    s["active_positions"] = active
    s["max_concurrent_positions"] = settings.POLYMARKET_MAX_OPEN_POSITIONS
    s["mode_guard"] = mode_guard.get("polymarket").to_dict()
    return s


@router.post("/polymarket/paper/bot/start")
async def polymarket_paper_bot_start():
    await poly_paper_bot.start()
    return poly_paper_bot.status()


@router.post("/polymarket/paper/bot/stop")
async def polymarket_paper_bot_stop():
    await poly_paper_bot.stop()
    return poly_paper_bot.status()


@router.get("/polymarket/paper/bot/status")
async def polymarket_paper_bot_status(session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(PolyTrade).where(PolyTrade.status == "OPEN", PolyTrade.mode == "paper")
    )
    active = len(list(res.scalars().all()))
    s = poly_paper_bot.status()
    s["active_positions"] = active
    return s


@router.post("/polymarket/live/bot/start")
async def polymarket_live_bot_start(session: AsyncSession = Depends(get_session)):
    preflight = await get_live_preflight(retries=2)
    if not preflight.get("ok"):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "live preflight failed",
                "preflight": preflight,
            },
        )

    await poly_live_bot.start()
    return poly_live_bot.status()


@router.post("/polymarket/live/bot/stop")
async def polymarket_live_bot_stop():
    await poly_live_bot.stop()
    return poly_live_bot.status()


@router.get("/polymarket/live/bot/status")
async def polymarket_live_bot_status(session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(PolyTrade).where(PolyTrade.status == "OPEN", PolyTrade.mode == "live")
    )
    active = len(list(res.scalars().all()))
    s = poly_live_bot.status()
    s["active_positions"] = active
    return s


class ModeRequestBody(BaseModel):
    platform: str


class ModeConfirmBody(BaseModel):
    platform: str
    passcode: str
    limits: dict | None = None


class KillSwitchBody(BaseModel):
    platform: str
    enabled: bool


class ModeSetPaperBody(BaseModel):
    platform: str


class PolyVaultResetBody(BaseModel):
    passcode: str


class PolyVaultSetCapBody(BaseModel):
    passcode: str
    cap_usd: float


class PolyVaultUnlockBody(BaseModel):
    passcode: str
    amount: float


class PolyLiveVaultSetCapBody(BaseModel):
    passcode: str
    cap_usd: float


class PolyLiveVaultUnlockBody(BaseModel):
    passcode: str
    amount: float


class PolyLiveWithdrawBody(BaseModel):
    passcode: str
    amount_usd: float | None = None
    requested_by: str = "manual"


class PolyLiveRetryWithdrawBody(BaseModel):
    passcode: str


class PolyLiveAutoWithdrawToggleBody(BaseModel):
    passcode: str
    enabled: bool


class PolyLiveTradeDeleteBody(BaseModel):
    passcode: str
    trade_id: str


@router.get("/mode/status")
async def mode_status():
    return mode_guard.snapshot()


@router.post("/mode/request-live")
async def mode_request_live(body: ModeRequestBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket", "candle"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    result = mode_guard.request_live(platform)  # type: ignore[arg-type]
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "request failed"))
    return result


@router.post("/mode/confirm-live")
async def mode_confirm_live(body: ModeConfirmBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket", "candle"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    result = mode_guard.confirm_live(platform, body.passcode, body.limits)  # type: ignore[arg-type]
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "confirm failed"))
    return result


@router.post("/mode/set-paper")
async def mode_set_paper(body: ModeSetPaperBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket", "candle"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    return mode_guard.set_paper(platform)  # type: ignore[arg-type]


@router.post("/mode/kill-switch")
async def mode_kill_switch(body: KillSwitchBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket", "candle"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    return mode_guard.set_kill_switch(platform, body.enabled)  # type: ignore[arg-type]


async def _polymarket_wallet_by_mode(current_mode: str, session: AsyncSession) -> dict:
    is_live = current_mode == "live"
    live_error = None
    w = await poly_wallet.get_wallet(session)
    live_trade_balance = float(w.live_trade_balance or 0.0)
    live_vault_balance = float(w.live_vault_balance or 0.0)
    live_actual_balance = live_trade_balance + live_vault_balance

    # Stats are always scoped to the current mode so live and paper don't bleed
    # into each other. Paper balance still comes from the simulated wallet
    # table; live balance comes from the on-chain CLOB call.
    if is_live:
        from app.services.poly_live import get_live_client
        if get_live_client() is None:
            live_error = "Missing or invalid POLYMARKET_PRIVATE_KEY"
            balance = 0.0
        else:
            balance = await get_live_balance()
            bal_err = get_live_balance_error()
            if bal_err:
                live_error = bal_err
            if settings.POLY_LIVE_VAULT_ENABLED:
                w, _ = await sweep_live_excess_if_needed(session, w, balance)
                await session.commit()
                live_trade_balance = float(w.live_trade_balance or 0.0)
                live_vault_balance = float(w.live_vault_balance or 0.0)
                live_actual_balance = live_trade_balance + live_vault_balance
            else:
                # Keep live wallet fields aligned with real CLOB balance even when
                # vaulting is disabled so the live UI never shows stale zeroes.
                live_trade_balance = float(balance)
                live_vault_balance = 0.0
                live_actual_balance = float(balance)
    else:
        balance = w.balance

    exclude_ids = {
        x.strip()
        for x in str(getattr(settings, "POLYMARKET_LIVE_EXCLUDE_TRADE_IDS", "") or "").split(",")
        if x.strip()
    }

    closed_q = select(PolyTrade).where(
        PolyTrade.mode == current_mode,
        PolyTrade.status.like("CLOSED%"),
    )
    closed = (await session.execute(closed_q)).scalars().all()
    if is_live and exclude_ids:
        closed = [t for t in closed if t.id not in exclude_ids]
    total_pnl = float(sum((t.pnl or 0.0) for t in closed))
    wins = sum(1 for t in closed if (t.pnl or 0.0) > 0)
    losses = sum(1 for t in closed if (t.pnl or 0.0) < 0)
    total_trades = len(closed)
    win_rate = (wins / total_trades * 100) if total_trades else 0.0

    common = {
        "mode": "live" if is_live else "paper",
        "balance": round(balance, 4),
        "force_mode_a": bool(settings.POLYMARKET_FORCE_MODE_A),
        "total_pnl": round(total_pnl, 4),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
    }
    if is_live:
        live_funded = (live_trade_balance > 0.0) if bool(settings.POLY_LIVE_VAULT_ENABLED) else (live_actual_balance > 0.0)
        return {
            **common,
            "trade_balance": round(live_trade_balance, 4),
            "vault_balance": round(live_vault_balance, 4),
            "actual_balance": round(live_actual_balance, 4),
            "trade_cap_usd": round(float(w.live_trade_cap_usd or settings.POLY_LIVE_TRADE_CAP_USD), 4),
            "vault_locked": True,
            "vault_sweeps_count": int(w.live_vault_sweeps_count or 0),
            "last_sweep_at": (w.live_last_sweep_at.isoformat() if w.live_last_sweep_at else None),
            "last_withdraw_at": (w.live_last_withdraw_at.isoformat() if w.live_last_withdraw_at else None),
            "withdrawn_total": round(float(w.live_withdrawn_total or 0.0), 4),
            "vault_enabled": bool(settings.POLY_LIVE_VAULT_ENABLED),
            "live_funded": bool(live_funded),
            "auto_withdraw_enabled": bool(_poly_live_auto_withdraw_enabled()),
            "live_error": live_error,
        }
    return {
        **common,
        "trade_balance": round(w.trade_balance, 4),
        "vault_balance": round(w.vault_balance, 4),
        "actual_balance": round(w.trade_balance + w.vault_balance, 4),
        "trade_cap_usd": round(w.trade_cap_usd, 4),
        "vault_locked": True,
        "vault_sweeps_count": int(w.vault_sweeps_count or 0),
        "last_sweep_at": (w.last_sweep_at.isoformat() if w.last_sweep_at else None),
    }


@router.get("/polymarket/wallet")
async def polymarket_wallet_get(session: AsyncSession = Depends(get_session)):
    # Backward-compatible legacy endpoint: now follows current mode.
    state = mode_guard.get("polymarket")
    mode = "live" if state.mode == "live_armed" else "paper"
    return await _polymarket_wallet_by_mode(mode, session)


@router.get("/polymarket/paper/wallet")
async def polymarket_paper_wallet_get(session: AsyncSession = Depends(get_session)):
    return await _polymarket_wallet_by_mode("paper", session)


@router.get("/polymarket/live/wallet")
async def polymarket_live_wallet_get(session: AsyncSession = Depends(get_session)):
    return await _polymarket_wallet_by_mode("live", session)


@router.post("/polymarket/vault/reset")
async def polymarket_vault_reset(body: PolyVaultResetBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    w = await poly_wallet.reset_vault(session)
    await session.commit()
    await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
    return {
        "ok": True,
        "trade_balance": round(w.trade_balance, 4),
        "vault_balance": round(w.vault_balance, 4),
        "actual_balance": round(w.trade_balance + w.vault_balance, 4),
    }


@router.post("/polymarket/vault/set-cap")
async def polymarket_vault_set_cap(body: PolyVaultSetCapBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    w, swept = await poly_wallet.set_trade_cap(session, body.cap_usd)
    await session.commit()
    await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
    return {
        "ok": True,
        "trade_cap_usd": round(w.trade_cap_usd, 4),
        "swept": round(swept, 4),
        "trade_balance": round(w.trade_balance, 4),
        "vault_balance": round(w.vault_balance, 4),
    }


@router.post("/polymarket/vault/unlock-to-trade")
async def polymarket_vault_unlock_to_trade(body: PolyVaultUnlockBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    w = await poly_wallet.unlock_to_trade(session, body.amount)
    await session.commit()
    await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
    return {
        "ok": True,
        "trade_balance": round(w.trade_balance, 4),
        "vault_balance": round(w.vault_balance, 4),
        "actual_balance": round(w.trade_balance + w.vault_balance, 4),
    }


@router.post("/polymarket/live/vault/set-cap")
async def polymarket_live_vault_set_cap(body: PolyLiveVaultSetCapBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    w = await poly_wallet.get_wallet(session)
    w.live_trade_cap_usd = round(max(0.01, float(body.cap_usd)), 4)
    if settings.POLY_LIVE_VAULT_ENABLED:
        balance = await get_live_balance()
        w, swept = await sweep_live_excess_if_needed(session, w, balance)
        if swept > 0:
            await bus.publish("agent:log", {"level": "INFO", "message": f"LIVE_VAULT_SWEEP +${swept:.2f}", "metadata": {"platform": "polymarket"}, "ts": datetime.now(timezone.utc).isoformat()})
    await session.commit()
    return {"ok": True, "live_trade_cap_usd": w.live_trade_cap_usd, "live_trade_balance": w.live_trade_balance, "live_vault_balance": w.live_vault_balance}


@router.post("/polymarket/live/vault/unlock-to-trade")
async def polymarket_live_vault_unlock_to_trade(body: PolyLiveVaultUnlockBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    w = await poly_wallet.get_wallet(session)
    move = round(max(0.0, min(float(body.amount), float(w.live_vault_balance or 0.0))), 4)
    w.live_vault_balance = round(float(w.live_vault_balance or 0.0) - move, 4)
    w.live_trade_balance = round(float(w.live_trade_balance or 0.0) + move, 4)
    session.add(PolyVaultEvent(event_type="UNLOCK", amount_usd=move, meta_json={"scope": "live_manual_unlock"}))
    await session.commit()
    return {
        "ok": True,
        "moved": move,
        "live_trade_balance": round(w.live_trade_balance, 4),
        "live_vault_balance": round(w.live_vault_balance, 4),
    }


@router.post("/polymarket/live/vault/withdraw")
async def polymarket_live_vault_withdraw(body: PolyLiveWithdrawBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    state = mode_guard.get("polymarket")
    if state.kill_switch:
        raise HTTPException(status_code=409, detail="kill switch is ON")
    w = await poly_wallet.get_wallet(session)
    amount = float(body.amount_usd or 0.0)
    if amount <= 0:
        candidate = round(float(w.live_vault_balance or 0.0) - float(settings.POLY_LIVE_VAULT_KEEP_BUFFER_USD), 4)
        amount = candidate
    amount = round(max(amount, 0.0), 4)
    if amount < float(settings.POLY_LIVE_MIN_WITHDRAW_USD):
        raise HTTPException(status_code=400, detail="amount below min withdraw")
    if amount > float(w.live_vault_balance or 0.0):
        raise HTTPException(status_code=400, detail="insufficient live vault balance")
    if await has_active_withdraw_job(session):
        raise HTTPException(status_code=409, detail="active withdraw job exists")
    job = await request_withdraw_job(session, amount_usd=amount, requested_by=body.requested_by or "manual")
    await session.flush()
    await execute_withdraw_job(session, job, w)
    await session.commit()
    return {"ok": True, "job_id": job.id, "status": job.status, "tx_hash": job.tx_hash, "error_message": job.error_message}


@router.get("/polymarket/live/vault/withdraw-jobs")
async def polymarket_live_vault_withdraw_jobs(limit: int = 50, session: AsyncSession = Depends(get_session)):
    q = select(PolyWithdrawJob).order_by(desc(PolyWithdrawJob.created_at)).limit(max(1, min(limit, 200)))
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": r.id,
            "amount_usd": r.amount_usd,
            "status": r.status,
            "idempotency_key": r.idempotency_key,
            "tx_hash": r.tx_hash,
            "error_message": r.error_message,
            "attempts": r.attempts,
            "requested_by": r.requested_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in rows
    ]


@router.post("/polymarket/live/vault/withdraw-jobs/{job_id}/retry")
async def polymarket_live_vault_withdraw_retry(job_id: str, body: PolyLiveRetryWithdrawBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    state = mode_guard.get("polymarket")
    if state.kill_switch:
        raise HTTPException(status_code=409, detail="kill switch is ON")
    res = await session.execute(select(PolyWithdrawJob).where(PolyWithdrawJob.id == job_id))
    job = res.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="withdraw job not found")
    w = await poly_wallet.get_wallet(session)
    await execute_withdraw_job(session, job, w)
    await session.commit()
    return {"ok": True, "job_id": job.id, "status": job.status, "attempts": job.attempts, "tx_hash": job.tx_hash, "error_message": job.error_message}


@router.post("/polymarket/live/vault/auto-withdraw/toggle")
async def polymarket_live_auto_withdraw_toggle(body: PolyLiveAutoWithdrawToggleBody):
    global _POLY_LIVE_AUTO_WITHDRAW_OVERRIDE
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    _POLY_LIVE_AUTO_WITHDRAW_OVERRIDE = bool(body.enabled)
    return {"ok": True, "enabled": bool(_POLY_LIVE_AUTO_WITHDRAW_OVERRIDE)}


@router.post("/polymarket/live/trades/delete")
async def polymarket_live_trade_delete(body: PolyLiveTradeDeleteBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    res = await session.execute(
        select(PolyTrade).where(PolyTrade.id == body.trade_id, PolyTrade.mode == "live")
    )
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="live trade not found")
    await session.delete(t)
    await session.commit()
    return {"ok": True, "deleted_id": body.trade_id}


@router.get("/polymarket/live/health")
async def polymarket_live_health():
    """Quick connectivity check for the live CLOB client."""
    return await get_live_preflight(retries=2)


@router.get("/polymarket/live/preflight")
async def polymarket_live_preflight():
    return await get_live_preflight(retries=2)


@router.get("/polymarket/trades")
async def polymarket_trades(
    status: str = "open",
    mode: str | None = None,
    limit: int = 50,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
):
    current_mode = (mode or "").strip().lower()
    if current_mode not in {"paper", "live"}:
        current_mode = "paper"
    q = select(PolyTrade).where(PolyTrade.mode == current_mode)
    if status == "open":
        q = q.where(PolyTrade.status == "OPEN")
    elif status == "closed":
        q = q.where(PolyTrade.status.like("CLOSED%"))
    q = q.order_by(desc(PolyTrade.opened_at)).offset(max(page - 1, 0) * limit).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    if current_mode == "live":
        exclude_ids = {
            x.strip()
            for x in str(getattr(settings, "POLYMARKET_LIVE_EXCLUDE_TRADE_IDS", "") or "").split(",")
            if x.strip()
        }
        if exclude_ids:
            rows = [t for t in rows if t.id not in exclude_ids]
    return [_poly_trade_dict(t) for t in rows]


@router.get("/polymarket/trades/summary")
async def polymarket_trades_summary(mode: str | None = None, session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    start_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    current_mode = (mode or "").strip().lower()
    if current_mode not in {"paper", "live"}:
        current_mode = "paper"
    if current_mode == "live":
        exclude_ids = {
            x.strip()
            for x in str(getattr(settings, "POLYMARKET_LIVE_EXCLUDE_TRADE_IDS", "") or "").split(",")
            if x.strip()
        }
        rows = (await session.execute(select(PolyTrade).where(PolyTrade.mode == current_mode))).scalars().all()
        if exclude_ids:
            rows = [t for t in rows if t.id not in exclude_ids]
        total = len(rows)
        open_count = sum(1 for t in rows if t.status == "OPEN")
        closed_count = sum(1 for t in rows if (t.status or "").startswith("CLOSED"))
        today_opened = sum(1 for t in rows if t.opened_at and t.opened_at >= start_utc)
        today_closed = sum(1 for t in rows if t.closed_at and t.closed_at >= start_utc)
    else:
        total = int((await session.execute(select(func.count(PolyTrade.id)).where(PolyTrade.mode == current_mode))).scalar_one() or 0)
        open_count = int((await session.execute(select(func.count(PolyTrade.id)).where(PolyTrade.mode == current_mode, PolyTrade.status == "OPEN"))).scalar_one() or 0)
        closed_count = int((await session.execute(select(func.count(PolyTrade.id)).where(PolyTrade.mode == current_mode, PolyTrade.status.like("CLOSED%")))).scalar_one() or 0)
        today_opened = int((await session.execute(select(func.count(PolyTrade.id)).where(PolyTrade.mode == current_mode, PolyTrade.opened_at >= start_utc))).scalar_one() or 0)
        today_closed = int((await session.execute(select(func.count(PolyTrade.id)).where(PolyTrade.mode == current_mode, PolyTrade.closed_at.is_not(None), PolyTrade.closed_at >= start_utc))).scalar_one() or 0)
    return {
        "total_count": total,
        "open_count": open_count,
        "closed_count": closed_count,
        "today_opened_count": today_opened,
        "today_closed_count": today_closed,
    }


@router.get("/polymarket/markets")
async def polymarket_markets(limit: int = 30):
    poly = get_polymarket()
    markets = await poly.get_markets(limit=limit)
    return [
        {
            "id": m.id,
            "title": m.title,
            "yes_price": m.yes_price,
            "no_price": m.no_price,
            "volume": m.volume,
            "time_to_close_seconds": m.close_time_seconds,
            "slug": str((m.raw or {}).get("slug") or ""),
            "event_slug": str((((m.raw or {}).get("events") or [{}])[0].get("slug") or "")),
        }
        for m in markets
    ]


@router.get("/polymarket/logs")
async def polymarket_logs(limit: int = 100, mode: str | None = None, session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(BotLog).order_by(desc(BotLog.created_at)).limit(500)
    )
    rows = res.scalars().all()
    wanted_kind = None
    m = (mode or "").strip().lower()
    if m in {"paper", "live"}:
        wanted_kind = m
    poly_rows = [
        r for r in rows
        if (r.metadata_json or {}).get("platform") == "polymarket"
        and (wanted_kind is None or (r.metadata_json or {}).get("bot_kind") == wanted_kind)
    ][:limit]
    return [
        {
            "id": r.id,
            "level": r.level,
            "message": r.message,
            "ts": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reversed(poly_rows)
    ]


@router.get("/polymarket/candles")
async def polymarket_candles(
    interval: str = "5m",
    limit: int = 80,
    symbol: str = "BTCUSDT",
):
    interval = interval.lower().strip()
    if interval not in {"5m", "15m"}:
        raise HTTPException(status_code=400, detail="interval must be 5m or 15m")
    limit = max(20, min(300, int(limit)))
    sym = symbol.upper().strip()
    out = []
    last_err = None
    # Primary source: Binance.
    try:
        async with httpx.AsyncClient(timeout=12.0) as h:
            r = await h.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": sym, "interval": interval, "limit": limit},
            )
            r.raise_for_status()
            rows = r.json()
        for row in rows:
            out.append(
                {
                    "t": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        return {"symbol": sym, "interval": interval, "source": "binance", "candles": out}
    except Exception as e:
        last_err = str(e)

    # Fallback source: Coinbase Exchange candles.
    # Coinbase format: [time, low, high, open, close, volume]
    try:
        granularity = 300 if interval == "5m" else 900
        product = "BTC-USD" if sym == "BTCUSDT" else "ETH-USD"
        async with httpx.AsyncClient(timeout=12.0) as h:
            r = await h.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={"granularity": granularity},
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            rows = r.json()
        rows = rows[:limit]
        for row in reversed(rows):
            out.append(
                {
                    "t": int(row[0]) * 1000,
                    "open": float(row[3]),
                    "high": float(row[2]),
                    "low": float(row[1]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        return {"symbol": product, "interval": interval, "source": "coinbase", "candles": out}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"candle fetch failed: primary={last_err}; fallback={e}")


# ---------- Agent ----------
class AnalyzeBody(BaseModel):
    market_id: str


@router.post("/agent/analyze")
async def agent_analyze(body: AnalyzeBody):
    kalshi = get_kalshi()
    market = await kalshi.get_market(body.market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    a = await analyze_market(market)
    return {
        "score": a.score,
        "action": a.action,
        "confidence": a.confidence,
        "entry_price": a.entry_price,
        "target_exit_price": a.target_exit_price,
        "stop_loss_price": a.stop_loss_price,
        "reasoning": a.reasoning,
        "knowledge_sources": a.knowledge_sources,
        "ai_review": a.raw.get("ai_review"),
    }


@router.get("/agent/claude/health")
async def agent_claude_health():
    return await asyncio.to_thread(check_claude_health)


@router.get("/agent/gemini/health")
async def agent_gemini_health():
    return await asyncio.to_thread(check_gemini_health)


@router.get("/agent/intel/health")
async def agent_intel_health():
    probe = Market(
        ticker="INTEL-HEALTH",
        title="Will external intel pipeline return healthy signals?",
        category="System",
        yes_price=0.5,
        no_price=0.5,
        volume=1000,
        open_interest=500,
        close_time_seconds=3600,
        raw={},
    )
    intel = await gather_market_intel(probe)
    payload = intel.as_dict()
    payload["ok"] = not payload.get("block_trade", False)
    return payload


@router.get("/risk/limits")
async def risk_limits():
    return {
        "kalshi": {
            "max_daily_loss_usd": settings.KALSHI_MAX_DAILY_LOSS_USD,
            "max_open_positions": settings.MAX_CONCURRENT_POSITIONS,
            "min_trade_score": settings.MIN_TRADE_SCORE,
        },
        "polymarket": {
            "max_daily_loss_usd": settings.POLYMARKET_MAX_DAILY_LOSS_USD,
            "max_open_positions": settings.POLYMARKET_MAX_OPEN_POSITIONS,
            "min_trade_score": settings.POLYMARKET_MIN_SCORE,
            "mode_a_max_underdog_price": settings.POLYMARKET_MODE_A_MAX_UNDERDOG_PRICE,
            "force_mode_a": settings.POLYMARKET_FORCE_MODE_A,
            "time_window_seconds": [
                settings.POLYMARKET_MIN_TIME_TO_CLOSE_SECONDS,
                settings.POLYMARKET_MAX_TIME_TO_CLOSE_SECONDS,
            ],
        },
    }


@router.get("/risk/reconcile")
async def risk_reconcile(session: AsyncSession = Depends(get_session)):
    kalshi = await reconcile_kalshi_paper(session)
    polymarket = await reconcile_polymarket_paper(session)
    return {
        "ok": bool(kalshi.get("ok")) and bool(polymarket.get("ok")),
        "platforms": {
            "kalshi": kalshi,
            "polymarket": polymarket,
        },
    }


@router.get("/risk/canary-status")
async def risk_canary_status(session: AsyncSession = Depends(get_session)):
    kalshi_mode = mode_guard.get("kalshi").mode
    polymarket_mode = mode_guard.get("polymarket").mode
    k_ok, k_reason, k_meta = await check_kalshi_canary(
        session, mode=kalshi_mode, order_usd=settings.TRADE_AMOUNT_USD
    )
    p_ok, p_reason, p_meta = await check_polymarket_canary(
        session, mode=polymarket_mode, order_usd=settings.POLYMARKET_TRADE_AMOUNT_USD
    )
    return {
        "enabled": settings.LIVE_CANARY_ENABLED,
        "kalshi": {"mode": kalshi_mode, "ok": k_ok, "reason": k_reason, "meta": k_meta},
        "polymarket": {"mode": polymarket_mode, "ok": p_ok, "reason": p_reason, "meta": p_meta},
        "limits": {
            "kalshi": {
                "max_order_usd": settings.KALSHI_CANARY_MAX_ORDER_USD,
                "max_new_trades_per_day": settings.KALSHI_CANARY_MAX_NEW_TRADES_PER_DAY,
                "max_total_exposure_usd": settings.KALSHI_CANARY_MAX_TOTAL_EXPOSURE_USD,
            },
            "polymarket": {
                "max_order_usd": settings.POLYMARKET_CANARY_MAX_ORDER_USD,
                "max_new_trades_per_day": settings.POLYMARKET_CANARY_MAX_NEW_TRADES_PER_DAY,
                "max_total_exposure_usd": settings.POLYMARKET_CANARY_MAX_TOTAL_EXPOSURE_USD,
            },
        },
    }


# ---------- Bots aggregate (Phase 5) ----------
@router.get("/bots")
async def bots_aggregate(session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    start_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    k_active = int((await session.execute(
        select(func.count(Trade.id)).where(Trade.status == "OPEN")
    )).scalar_one() or 0)
    k_today_opened = int((await session.execute(
        select(func.count(Trade.id)).where(Trade.opened_at >= start_utc)
    )).scalar_one() or 0)
    k_today_pnl = float((await session.execute(
        select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
            Trade.closed_at.is_not(None), Trade.closed_at >= start_utc
        )
    )).scalar_one() or 0.0)
    k_wallet = await wallet.get_wallet(session)
    k_status = bot.status()

    p_active = int((await session.execute(
        select(func.count(PolyTrade.id)).where(PolyTrade.status == "OPEN")
    )).scalar_one() or 0)
    p_today_opened = int((await session.execute(
        select(func.count(PolyTrade.id)).where(PolyTrade.opened_at >= start_utc)
    )).scalar_one() or 0)
    p_today_pnl = float((await session.execute(
        select(func.coalesce(func.sum(PolyTrade.pnl), 0.0)).where(
            PolyTrade.closed_at.is_not(None), PolyTrade.closed_at >= start_utc
        )
    )).scalar_one() or 0.0)
    p_wallet = await poly_wallet.get_wallet(session)
    p_status = poly_bot.status()
    
    p_balance = p_wallet.balance
    if mode_guard.get("polymarket").mode == "live_armed":
        p_balance = await get_live_balance()

    return {
        "kalshi": {
            "platform": "kalshi",
            "status": k_status.get("status"),
            "state": k_status.get("state"),
            "uptime_seconds": k_status.get("uptime_seconds"),
            "scanned_today": k_status.get("scanned_markets_today", 0),
            "last_scan_count": k_status.get("last_scan_count", 0),
            "last_candidate_count": k_status.get("last_candidate_count", 0),
            "active_positions": k_active,
            "max_concurrent_positions": settings.MAX_CONCURRENT_POSITIONS,
            "today_opened": k_today_opened,
            "today_pnl": round(k_today_pnl, 4),
            "wallet": {
                "balance": round(k_wallet.balance, 4),
                "total_pnl": round(k_wallet.total_pnl, 4),
                "total_trades": k_wallet.total_trades,
                "wins": k_wallet.wins,
                "losses": k_wallet.losses,
            },
            "mode_guard": mode_guard.get("kalshi").to_dict(),
        },
        "polymarket": {
            "platform": "polymarket",
            "status": p_status.get("status"),
            "state": p_status.get("state"),
            "uptime_seconds": p_status.get("uptime_seconds"),
            "scanned_today": p_status.get("scanned_markets_today", 0),
            "last_scan_count": p_status.get("last_scan_count", 0),
            "last_candidate_count": p_status.get("last_candidate_count", 0),
            "active_positions": p_active,
            "max_concurrent_positions": settings.POLYMARKET_MAX_OPEN_POSITIONS,
            "today_opened": p_today_opened,
            "today_pnl": round(p_today_pnl, 4),
            "wallet": {
                "balance": round(p_balance, 4),
                "total_pnl": round(p_wallet.total_pnl, 4),
                "total_trades": p_wallet.total_trades,
                "wins": p_wallet.wins,
                "losses": p_wallet.losses,
            },
            "mode_guard": mode_guard.get("polymarket").to_dict(),
        },
        "combined": {
            "today_pnl": round(k_today_pnl + p_today_pnl, 4),
            "today_opened": k_today_opened + p_today_opened,
            "active_positions": k_active + p_active,
            "balance": round(k_wallet.balance + p_balance, 4),
        },
    }


# ---------- Reports (Phase 5) ----------
def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.get("/reports/daily")
async def reports_daily(
    platform: str = "all",
    days: int = 14,
    session: AsyncSession = Depends(get_session),
):
    days = max(1, min(days, 90))
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=days - 1)
    platform = platform.lower()

    series: dict[str, list[dict]] = {}

    async def _build(model, label: str):
        if platform not in {"all", label}:
            return
        rows = (await session.execute(
            select(model.closed_at, model.pnl).where(
                model.closed_at.is_not(None), model.closed_at >= start
            )
        )).all()
        buckets: dict[str, dict[str, float]] = {}
        for d in range(days):
            day = (start + timedelta(days=d)).date().isoformat()
            buckets[day] = {"date": day, "pnl": 0.0, "wins": 0, "losses": 0, "trades": 0}
        for closed_at, pnl in rows:
            if closed_at is None:
                continue
            key = closed_at.date().isoformat()
            b = buckets.get(key)
            if b is None:
                continue
            b["pnl"] += float(pnl or 0.0)
            b["trades"] += 1
            if (pnl or 0.0) > 0:
                b["wins"] += 1
            elif (pnl or 0.0) < 0:
                b["losses"] += 1
        out = []
        for key in sorted(buckets):
            b = buckets[key]
            b["pnl"] = round(b["pnl"], 4)
            out.append(b)
        series[label] = out

    await _build(Trade, "kalshi")
    await _build(PolyTrade, "polymarket")

    combined = []
    if "kalshi" in series and "polymarket" in series:
        k_idx = {r["date"]: r for r in series["kalshi"]}
        for row in series["polymarket"]:
            k = k_idx.get(row["date"], {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
            combined.append({
                "date": row["date"],
                "pnl": round(row["pnl"] + k["pnl"], 4),
                "trades": row["trades"] + k["trades"],
                "wins": row["wins"] + k["wins"],
                "losses": row["losses"] + k["losses"],
            })

    return {
        "from": start.date().isoformat(),
        "to": now.date().isoformat(),
        "days": days,
        "series": series,
        "combined": combined,
    }


@router.get("/reports/export.csv")
async def reports_export_csv(
    platform: str = "all",
    status: str = "all",
    session: AsyncSession = Depends(get_session),
):
    platform = platform.lower()
    status = status.lower()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "platform", "id", "market_id", "market_title", "direction",
        "amount", "entry_price", "exit_price", "pnl", "status",
        "agent_score", "opened_at", "closed_at",
    ])

    async def _emit(model, label: str):
        if platform not in {"all", label}:
            return
        q = select(model).order_by(desc(model.opened_at))
        if status == "open":
            q = q.where(model.status == "OPEN")
        elif status == "closed":
            q = q.where(model.status.like("CLOSED%"))
        rows = (await session.execute(q)).scalars().all()
        for t in rows:
            w.writerow([
                label, t.id, t.market_id, t.market_title, t.direction,
                t.amount, t.entry_price, getattr(t, "exit_price", None),
                t.pnl, t.status, t.agent_score,
                t.opened_at.isoformat() if t.opened_at else "",
                t.closed_at.isoformat() if t.closed_at else "",
            ])

    await _emit(Trade, "kalshi")
    await _emit(PolyTrade, "polymarket")

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="amta_trades.csv"'},
    )


@router.get("/agent/logs")
async def agent_logs(limit: int = 50, session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(BotLog).order_by(desc(BotLog.created_at)).limit(limit)
    )
    return [
        {
            "id": l.id,
            "level": l.level,
            "message": l.message,
            "metadata": l.metadata_json,
            "created_at": l.created_at,
        }
        for l in res.scalars().all()
    ]


@router.post("/agent/knowledge/reload")
async def knowledge_reload():
    # Run in thread to avoid blocking event loop
    return await asyncio.to_thread(knowledge.ingest_directory)


@router.post("/agent/knowledge/upload")
async def knowledge_upload(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")

    payload: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        payload.append((f.filename or "upload.pdf", content))
    saved = await asyncio.to_thread(knowledge.save_uploaded_pdfs, payload)
    return saved


@router.get("/agent/knowledge/stats")
async def knowledge_stats():
    return knowledge.stats()


class KnowledgeQueryBody(BaseModel):
    text: str
    k: int = 5


@router.post("/agent/knowledge/query")
async def knowledge_query(body: KnowledgeQueryBody):
    """Run a similarity search against the KB. Returns the top-k chunks
    so you can sanity-check what the analyzer would see."""
    return {"hits": await asyncio.to_thread(knowledge.query, body.text, body.k)}


@router.post("/agent/knowledge/reset")
async def knowledge_reset():
    """Drop the Chroma collection so the next /reload re-ingests everything
    from scratch (e.g. after swapping embedding model)."""
    await asyncio.to_thread(knowledge.reset_index)
    return {"ok": True}


@router.get("/agent/knowledge/pdfs")
async def knowledge_pdfs():
    """List PDFs found on disk (so the UI can show what's available to ingest)."""
    paths = knowledge.list_pdfs(knowledge.settings.KNOWLEDGE_PDF_DIR)
    return {
        "dir": knowledge.settings.KNOWLEDGE_PDF_DIR,
        "files": [{"path": str(p), "name": p.name} for p in paths],
    }


# ---------- Markets ----------
@router.get("/markets")
async def markets_list(limit: int = 30):
    kalshi = get_kalshi()
    markets = await kalshi.get_liquid_markets(limit=limit, min_volume=0)
    if not markets:
        markets = await kalshi.get_markets(limit=limit, max_pages=50)
    return [
        {
            "ticker": m.ticker,
            "title": m.title,
            "category": m.category,
            "yes_price": m.yes_price,
            "no_price": m.no_price,
            "volume": m.volume,
            "open_interest": m.open_interest,
            "time_to_close_seconds": m.close_time_seconds,
        }
        for m in markets
    ]


# ---------- Strategies ----------
@router.get("/strategies")
async def strategies_list(session: AsyncSession = Depends(get_session)):
    active_ids = [s.id for s in strategies.get_active_list()]
    out = []
    for s in strategies.STRATEGIES:
        # per-strategy stats
        res = await session.execute(
            select(Trade).where(Trade.strategy_id == s.id)
        )
        trades = list(res.scalars().all())
        closed = [t for t in trades if t.status.startswith("CLOSED")]
        wins = sum(1 for t in closed if t.pnl > 0)
        total_pnl = round(sum(t.pnl for t in closed), 4)
        out.append(
            {
                **strategies.public_view(s),
                "is_active": s.id in active_ids,
                "trades_total": len(trades),
                "trades_open": len(trades) - len(closed),
                "trades_closed": len(closed),
                "wins": wins,
                "win_rate": round(wins / len(closed) * 100, 2) if closed else 0.0,
                "total_pnl": total_pnl,
            }
        )
    return {
        "strategies": out,
        "active_ids": active_ids,
        # Back-compat: first active id (for any client still expecting single).
        "active_id": active_ids[0] if active_ids else None,
    }


@router.get("/strategies/{strategy_id}/preview")
async def strategy_preview(strategy_id: str):
    s = strategies.get_strategy(strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    kalshi = get_kalshi()
    markets = await kalshi.get_liquid_markets(limit=120, min_volume=10)
    if not markets:
        markets = await kalshi.get_markets(limit=120, min_volume=10, max_pages=20)
    matched = [m for m in markets if s.filter(m) and m.close_time_seconds > 5 * 60]
    matched.sort(key=s.sort_key, reverse=True)
    return {
        "strategy": strategies.public_view(s),
        "matches": len(matched),
        "markets": [
            {
                "ticker": m.ticker,
                "title": m.title,
                "yes_price": m.yes_price,
                "volume": m.volume,
                "close_time_seconds": m.close_time_seconds,
            }
            for m in matched[:20]
        ],
    }


class StrategyActivateBody(BaseModel):
    strategy_id: str | None = None
    active: bool = True  # true = activate, false = deactivate


@router.post("/strategies/activate")
async def strategy_activate(body: StrategyActivateBody):
    """Toggle a strategy on/off. Multiple strategies may be active at once;
    when several match the same market, the first activated wins."""
    if body.strategy_id is None:
        # Treat as 'deactivate all'
        strategies.deactivate_all()
        await bus.publish("strategy:changed", {"active_ids": []})
        return {"active_ids": []}
    try:
        if body.active:
            strategies.activate(body.strategy_id)
        else:
            strategies.deactivate(body.strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    active_ids = [s.id for s in strategies.get_active_list()]
    await bus.publish("strategy:changed", {"active_ids": active_ids})
    return {"active_ids": active_ids}


@router.post("/strategies/deactivate_all")
async def strategy_deactivate_all():
    strategies.deactivate_all()
    await bus.publish("strategy:changed", {"active_ids": []})
    return {"active_ids": []}


# ---------- Quick Trade ----------
class QuickTradeBody(BaseModel):
    market_id: str
    direction: str | None = None  # "YES"/"NO"; if None, use AI suggestion
    override: bool = False        # bypass min-score gate


_QUICK_TRADE_REJECTION_MESSAGES = {
    exec_svc.OpenTradeRejectReason.ACTION_NOT_BUY:
        "trade rejected: analyzer action is not a BUY signal for this market",
    exec_svc.OpenTradeRejectReason.SCORE_BELOW_THRESHOLD:
        "trade rejected: score below configured threshold (enable override score gate to bypass)",
    exec_svc.OpenTradeRejectReason.DUPLICATE_MARKET_POSITION:
        "trade rejected: an OPEN position already exists for this market",
    exec_svc.OpenTradeRejectReason.DUPLICATE_EVENT_POSITION:
        "trade rejected: an OPEN position already exists for this event",
    exec_svc.OpenTradeRejectReason.POSITION_CAP_REACHED:
        "trade rejected: max concurrent position cap reached",
    exec_svc.OpenTradeRejectReason.INSUFFICIENT_BALANCE:
        "trade rejected: insufficient paper wallet balance",
}


@router.post("/quick-trade/preview")
async def quick_trade_preview(body: QuickTradeBody):
    """Run analyzer on the requested market without opening a position."""
    kalshi = get_kalshi()
    market = await kalshi.get_market(body.market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    a = await analyze_market(market)
    return {
        "market": {
            "ticker": market.ticker,
            "title": market.title,
            "category": market.category,
            "yes_price": market.yes_price,
            "no_price": market.no_price,
            "volume": market.volume,
            "time_to_close_seconds": market.close_time_seconds,
        },
        "analysis": {
            "score": a.score,
            "action": a.action,
            "confidence": a.confidence,
            "entry_price": a.entry_price,
            "target_exit_price": a.target_exit_price,
            "stop_loss_price": a.stop_loss_price,
            "reasoning": a.reasoning,
            "knowledge_sources": a.knowledge_sources,
            "ai_review": a.raw.get("ai_review"),
        },
    }


@router.post("/quick-trade/execute")
async def quick_trade_execute(
    body: QuickTradeBody, session: AsyncSession = Depends(get_session)
):
    """Open a $1 position immediately. If `override=true`, ignore min-score gate.
    If `direction` is provided, force it; otherwise use the analyzer's BUY_*."""
    from app.agent.analyzer import Analysis  # local import to avoid cycle

    kalshi = get_kalshi()
    market = await kalshi.get_market(body.market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    a = await analyze_market(market)

    # Honor manual direction override
    direction = (body.direction or "").upper()
    if direction in ("YES", "NO"):
        analysis = Analysis(
            score=max(a.score, 65),  # ensure passes default gate when direction forced
            action=f"BUY_{direction}",
            confidence=a.confidence,
            entry_price=market.yes_price if direction == "YES" else market.no_price,
            target_exit_price=a.target_exit_price,
            stop_loss_price=a.stop_loss_price,
            reasoning=f"manual {direction} override; " + a.reasoning,
            knowledge_sources=a.knowledge_sources,
            raw=a.raw,
        )
    else:
        analysis = a

    min_score = 0 if body.override else None
    trade, reject_reason = await exec_svc.open_trade_with_reason(
        session, kalshi, market, analysis,
        min_score=min_score,
        strategy_id=None,
        source="quick_trade",
    )
    if trade is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": reject_reason.value if reject_reason is not None else "unknown",
                "message": _QUICK_TRADE_REJECTION_MESSAGES.get(
                    reject_reason,
                    "trade rejected by execution guard",
                ),
            },
        )
    await session.commit()
    await bus.publish(
        "trade:opened",
        {
            "id": trade.id, "market_id": trade.market_id,
            "market_title": trade.market_title, "direction": trade.direction,
            "entry_price": trade.entry_price, "agent_score": trade.agent_score,
            "source": "quick_trade",
        },
    )
    return _trade_dict(trade)


# ---------- WebSocket ----------
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    q = bus.subscribe()
    try:
        await ws.send_text('{"event":"hello","data":{"ok":true}}')
        while True:
            msg = await q.get()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(q)


def _trade_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "market_id": t.market_id,
        "market_title": t.market_title,
        "category": t.category,
        "direction": t.direction,
        "amount": t.amount,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "target_exit_price": t.target_exit_price,
        "stop_loss_price": t.stop_loss_price,
        "pnl": t.pnl,
        "status": t.status,
        "agent_score": t.agent_score,
        "reasoning": t.reasoning,
        "strategy_id": t.strategy_id,
        "source": t.source,
        "opened_at": t.opened_at,
        "closed_at": t.closed_at,
    }


def _poly_trade_dict(t: PolyTrade) -> dict:
    return {
        "id": t.id,
        "market_id": t.market_id,
        "market_title": t.market_title,
        "direction": t.direction,
        "amount": t.amount,
        "entry_price": t.entry_price,
        "current_price": t.current_price,
        "exit_price": t.exit_price,
        "pnl": t.pnl,
        "status": t.status,
        "agent_score": t.agent_score,
        "reasoning": t.reasoning,
        "opened_at": t.opened_at,
        "closed_at": t.closed_at,
    }


async def _time_to_close_seconds(kalshi, market_id: str) -> int | None:
    now = time.time()
    cached = _MARKET_CLOSE_CACHE.get(market_id)
    if cached and now - cached[0] < _MARKET_CLOSE_CACHE_TTL_SECONDS:
        return cached[1]
    market = await kalshi.get_market(market_id)
    value = market.close_time_seconds if market is not None else None
    _MARKET_CLOSE_CACHE[market_id] = (now, value)
    return value


# ════════════════════════════════════════════════════════════════════
# CANDLE BOT — BTC/ETH crypto strategy with Binance data + execution
# ════════════════════════════════════════════════════════════════════


# ── TradingView webhook (AI advisory) ───────────────────────────────
@router.post("/tradingview/webhook")
async def tradingview_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    raw_json: dict = {}
    raw_text = ""
    try:
        raw_json = await request.json()
        if not isinstance(raw_json, dict):
            raw_json = {}
    except Exception:
        raw_json = {}
    if not raw_json:
        raw_text = (await request.body()).decode("utf-8", errors="ignore").strip()
        parsed_text = _tv_parse_text_message(raw_text)
        raw_json = {
            "symbol": parsed_text.get("symbol") or "BTCUSD",
            "action": parsed_text.get("action"),
            "qty": parsed_text.get("qty"),
            "meta": {"raw_message": raw_text},
        }
    body = TradingViewWebhookBody(**raw_json)

    # Optional shared secret check (header takes precedence over payload meta).
    expected = settings.TRADINGVIEW_WEBHOOK_SECRET.strip()
    provided = (request.headers.get("x-tv-secret") or "").strip()
    if not provided and isinstance(body.meta, dict):
        provided = str(body.meta.get("secret") or "").strip()
    if expected and provided != expected:
        raise HTTPException(status_code=401, detail="invalid tradingview webhook secret")

    action = (body.action or "").upper().strip()
    if not action:
        tv_decision = (body.decision or "").upper().strip()
        action = "SELL" if tv_decision in {"SELL", "SHORT"} else "BUY" if tv_decision in {"BUY", "LONG"} else ""
    symbol = _tv_normalize_symbol(body.symbol)
    timeframe = body.timeframe or "1"
    px = float(body.close) if body.close is not None else None
    if px is None:
        try:
            px = float(await binance_data.get_price(symbol))
        except Exception:
            px = None
    if px is None or px <= 0:
        raise HTTPException(status_code=400, detail="missing/invalid close price and price fetch failed")

    ai = await _tradingview_openai_decide(body)
    webhook_mode = (body.mode or "paper").lower()
    if webhook_mode not in {"paper", "live"}:
        webhook_mode = "paper"
    # user requested "direct short"; webhook keeps only short entries.
    short_only = True

    executed = {"mode": webhook_mode, "action": action or "NONE", "symbol": symbol, "price": px}
    wallet_state = None

    # Open/close paper positions from webhook signal.
    # SELL => open short (if no open short already for symbol)
    # BUY  => close open short(s) for symbol
    if webhook_mode == "paper":
        w = await _candle_ensure_wallet(session)
        open_rows = (
            await session.execute(
                select(CandleTrade).where(
                    CandleTrade.mode == "paper",
                    CandleTrade.symbol == symbol,
                    CandleTrade.status == "OPEN",
                )
            )
        ).scalars().all()

        if action == "SELL":
            if short_only:
                has_open_short = any(t.direction == "SHORT" for t in open_rows)
                if not has_open_short:
                    risk_pct = max(0.001, float(settings.CANDLE_RISK_PER_TRADE_PCT))
                    risk_usd = float(w.paper_balance) * risk_pct
                    sl_price = float(body.sl) if body.sl and body.sl > px else px * 1.004
                    tp_price = float(body.tp) if body.tp and body.tp < px else px * 0.992
                    stop_distance = max(1e-6, sl_price - px)
                    qty = float(body.qty) if body.qty and body.qty > 0 else (risk_usd / stop_distance)
                    notional = qty * px
                    cap_abs = float(settings.CANDLE_PAPER_MAX_NOTIONAL_USD)
                    cap_pct = float(settings.CANDLE_PAPER_MAX_NOTIONAL_PCT_EQUITY) * float(w.paper_balance)
                    cap = max(0.0, min(cap_abs, cap_pct if cap_pct > 0 else cap_abs))
                    if cap > 0 and notional > cap:
                        notional = cap
                        qty = notional / px
                    if notional <= 0 or notional > float(w.paper_balance):
                        executed["status"] = "rejected_insufficient_balance"
                    else:
                        t = CandleTrade(
                            id=str(uuid.uuid4()),
                            symbol=symbol,
                            interval=timeframe,
                            direction="SHORT",
                            qty=float(round(qty, 8)),
                            notional_usd=float(round(notional, 6)),
                            entry_price=px,
                            stop_loss=sl_price,
                            take_profit=tp_price,
                            current_price=px,
                            status="OPEN",
                            htf_bias="down",
                            setup_type="tv_webhook",
                            confidence=float(body.confidence or 0),
                            rr_target=2.0,
                            reasoning=f"TradingView SELL webhook; ai={ai.get('decision')}",
                            mode="paper",
                        )
                        session.add(t)
                        w.paper_balance = round(float(w.paper_balance) - float(notional), 6)
                        executed.update({"status": "opened_short", "trade_id": t.id, "qty": t.qty, "notional_usd": t.notional_usd})
                else:
                    executed["status"] = "skipped_short_already_open"
            else:
                executed["status"] = "skipped_not_short_mode"
        elif action == "BUY":
            closed = 0
            pnl_sum = 0.0
            for t in open_rows:
                if t.direction != "SHORT":
                    continue
                pnl = (t.entry_price - px) * t.qty
                t.current_price = px
                t.exit_price = px
                t.pnl_usd = round(float(pnl), 6)
                t.pnl_pct = round(((t.entry_price - px) / t.entry_price * 100.0) if t.entry_price else 0.0, 6)
                t.status = "CLOSED_TV_SIGNAL"
                t.closed_at = datetime.now(timezone.utc)
                w.paper_balance = round(float(w.paper_balance) + float(t.notional_usd) + float(pnl), 6)
                w.paper_total_pnl = round(float(w.paper_total_pnl) + float(pnl), 6)
                w.paper_total_trades = int(w.paper_total_trades or 0) + 1
                if pnl >= 0:
                    w.paper_wins = int(w.paper_wins or 0) + 1
                else:
                    w.paper_losses = int(w.paper_losses or 0) + 1
                closed += 1
                pnl_sum += float(pnl)
            executed.update({"status": "closed_shorts" if closed else "no_short_to_close", "closed_count": closed, "pnl_usd": round(pnl_sum, 6)})
        else:
            executed["status"] = "ignored_no_action"

        wallet_state = {
            "paper_balance": round(float(w.paper_balance or 0), 6),
            "paper_total_pnl": round(float(w.paper_total_pnl or 0), 6),
            "paper_total_trades": int(w.paper_total_trades or 0),
            "paper_wins": int(w.paper_wins or 0),
            "paper_losses": int(w.paper_losses or 0),
        }

    response = {
        "ok": True,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "time": body.time,
        "close": px,
        "tv_signal": {
            "decision": body.decision,
            "action": action,
            "confidence": body.confidence,
            "tp": body.tp,
            "sl": body.sl,
            "signal_id": body.signal_id,
            "strategy": body.strategy,
        },
        "ai_signal": ai,
        "execution": executed,
        "wallet": wallet_state,
    }

    # Persist a concise audit trail in bot_logs.
    session.add(BotLog(
        level="INFO",
        message=f"tradingview {body.symbol} -> {ai.get('decision')} conf={ai.get('confidence')}",
        metadata_json={
            "platform": "tradingview",
            "symbol": symbol,
            "timeframe": body.timeframe,
            "signal_id": body.signal_id,
            "strategy": body.strategy,
            "tv_decision": body.decision,
            "tv_action": action,
            "tv_confidence": body.confidence,
            "tv_tp": body.tp,
            "tv_sl": body.sl,
            "ai": ai,
            "execution": executed,
            "raw_text": raw_text[:500] if raw_text else None,
        },
    ))
    await session.commit()
    await bus.publish("tradingview:signal", response)
    return response


@router.post("/tradingview/test-order")
async def tradingview_test_order(
    body: TradingViewTestOrderBody,
    session: AsyncSession = Depends(get_session),
):
    """Manual test hook to simulate TradingView alerts without waiting for signals."""
    payload = TradingViewWebhookBody(
        symbol=body.symbol,
        timeframe=body.timeframe,
        close=body.close,
        action=(body.action or "").upper().strip(),
        mode=body.mode,
        confidence=body.confidence,
        tp=body.tp,
        sl=body.sl,
        strategy="manual_test_order",
        meta={"source": "manual_test_order"},
    )

    # Reuse the same execution behavior as /tradingview/webhook.
    action = payload.action or ""
    symbol = _tv_normalize_symbol(payload.symbol)
    px = float(payload.close) if payload.close is not None else None
    if px is None:
        try:
            px = float(await binance_data.get_price(symbol))
        except Exception:
            px = None
    if px is None or px <= 0:
        raise HTTPException(status_code=400, detail="missing/invalid close price and price fetch failed")

    webhook_mode = (payload.mode or "paper").lower()
    if webhook_mode not in {"paper", "live"}:
        webhook_mode = "paper"
    short_only = True

    ai = await _tradingview_openai_decide(payload)
    executed = {"mode": webhook_mode, "action": action or "NONE", "symbol": symbol, "price": px}
    wallet_state = None

    if webhook_mode == "paper":
        w = await _candle_ensure_wallet(session)
        open_rows = (
            await session.execute(
                select(CandleTrade).where(
                    CandleTrade.mode == "paper",
                    CandleTrade.symbol == symbol,
                    CandleTrade.status == "OPEN",
                )
            )
        ).scalars().all()

        if action == "SELL":
            has_open_short = any(t.direction == "SHORT" for t in open_rows)
            if short_only and not has_open_short:
                risk_pct = max(0.001, float(settings.CANDLE_RISK_PER_TRADE_PCT))
                risk_usd = float(w.paper_balance) * risk_pct
                sl_price = float(payload.sl) if payload.sl and payload.sl > px else px * 1.004
                tp_price = float(payload.tp) if payload.tp and payload.tp < px else px * 0.992
                stop_distance = max(1e-6, sl_price - px)
                qty = risk_usd / stop_distance
                notional = qty * px
                cap_abs = float(settings.CANDLE_PAPER_MAX_NOTIONAL_USD)
                cap_pct = float(settings.CANDLE_PAPER_MAX_NOTIONAL_PCT_EQUITY) * float(w.paper_balance)
                cap = max(0.0, min(cap_abs, cap_pct if cap_pct > 0 else cap_abs))
                if cap > 0 and notional > cap:
                    notional = cap
                    qty = notional / px
                if notional <= 0 or notional > float(w.paper_balance):
                    executed["status"] = "rejected_insufficient_balance"
                else:
                    t = CandleTrade(
                        id=str(uuid.uuid4()),
                        symbol=symbol,
                        interval=payload.timeframe or "1",
                        direction="SHORT",
                        qty=float(round(qty, 8)),
                        notional_usd=float(round(notional, 6)),
                        entry_price=px,
                        stop_loss=sl_price,
                        take_profit=tp_price,
                        current_price=px,
                        status="OPEN",
                        htf_bias="down",
                        setup_type="tv_manual_test",
                        confidence=float(payload.confidence or 0),
                        rr_target=2.0,
                        reasoning=f"Manual test SELL; ai={ai.get('decision')}",
                        mode="paper",
                    )
                    session.add(t)
                    w.paper_balance = round(float(w.paper_balance) - float(notional), 6)
                    executed.update({"status": "opened_short", "trade_id": t.id, "qty": t.qty, "notional_usd": t.notional_usd})
            else:
                executed["status"] = "skipped_short_already_open"
        elif action == "BUY":
            closed = 0
            pnl_sum = 0.0
            for t in open_rows:
                if t.direction != "SHORT":
                    continue
                pnl = (t.entry_price - px) * t.qty
                t.current_price = px
                t.exit_price = px
                t.pnl_usd = round(float(pnl), 6)
                t.pnl_pct = round(((t.entry_price - px) / t.entry_price * 100.0) if t.entry_price else 0.0, 6)
                t.status = "CLOSED_TV_SIGNAL"
                t.closed_at = datetime.now(timezone.utc)
                w.paper_balance = round(float(w.paper_balance) + float(t.notional_usd) + float(pnl), 6)
                w.paper_total_pnl = round(float(w.paper_total_pnl) + float(pnl), 6)
                w.paper_total_trades = int(w.paper_total_trades or 0) + 1
                if pnl >= 0:
                    w.paper_wins = int(w.paper_wins or 0) + 1
                else:
                    w.paper_losses = int(w.paper_losses or 0) + 1
                closed += 1
                pnl_sum += float(pnl)
            executed.update({"status": "closed_shorts" if closed else "no_short_to_close", "closed_count": closed, "pnl_usd": round(pnl_sum, 6)})
        else:
            raise HTTPException(status_code=400, detail="action must be SELL or BUY")

        wallet_state = {
            "paper_balance": round(float(w.paper_balance or 0), 6),
            "paper_total_pnl": round(float(w.paper_total_pnl or 0), 6),
            "paper_total_trades": int(w.paper_total_trades or 0),
            "paper_wins": int(w.paper_wins or 0),
            "paper_losses": int(w.paper_losses or 0),
        }

    session.add(BotLog(
        level="INFO",
        message=f"tradingview test-order {symbol} {action}",
        metadata_json={
            "platform": "tradingview",
            "symbol": symbol,
            "action": action,
            "execution": executed,
            "ai": ai,
            "source": "manual_test_order",
        },
    ))
    await session.commit()
    await bus.publish("tradingview:signal", {"test": True, "execution": executed})
    return {
        "ok": True,
        "test": True,
        "symbol": symbol,
        "close": px,
        "execution": executed,
        "wallet": wallet_state,
        "ai_signal": ai,
    }


@router.get("/tradingview/test-order/sell")
async def tradingview_test_order_sell(symbol: str = "BTCUSD", mode: str = "paper"):
    """Browser-friendly test: open short via GET."""
    payload = {"action": "SELL", "symbol": symbol, "mode": mode}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        r = await client.post("http://localhost:4000/api/tradingview/test-order", json=payload)
        return r.json()


@router.get("/tradingview/test-order/buy")
async def tradingview_test_order_buy(symbol: str = "BTCUSD", mode: str = "paper"):
    """Browser-friendly test: close short via GET."""
    payload = {"action": "BUY", "symbol": symbol, "mode": mode}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        r = await client.post("http://localhost:4000/api/tradingview/test-order", json=payload)
        return r.json()


# ── Market data (public, no auth) ────────────────────────────────────
class CandleBotStartBody(BaseModel):
    strategy_id: str


@router.get("/candle/strategies")
async def candle_strategies():
    return {
        "default": "sweep_bos_v1",
        "items": [
            {"id": k, "label": v} for k, v in candle_strat.SUPPORTED_STRATEGIES.items()
        ],
    }


@router.get("/candle/klines")
async def candle_klines(symbol: str = "BTCUSDT", interval: str = "5m", limit: int = 200):
    """Fetch OHLCV candles from Binance public API (proxied)."""
    klines = await binance_data.get_klines(symbol=symbol, interval=interval, limit=limit)
    return {"symbol": symbol.upper(), "interval": interval, "candles": klines}


@router.get("/candle/price")
async def candle_price(symbol: str = "BTCUSDT"):
    price = await binance_data.get_price(symbol)
    return {"symbol": symbol.upper(), "price": price}


@router.get("/binance/spot/assets")
async def binance_spot_assets():
    """Read-only Binance spot assets snapshot for sheet/dashboard sync."""
    usdt_free, balances, err = await binance_live.get_account_balance_safe()
    if err:
        return {
            "ok": False,
            "error": err,
            "assets": [],
            "totals": {"estimated_usdt": 0.0, "usdt_free": 0.0},
        }

    rows: list[dict] = []
    total_usdt_value = 0.0
    for asset, v in sorted(balances.items()):
        free = float(v.get("free", 0.0))
        locked = float(v.get("locked", 0.0))
        qty = free + locked
        if qty <= 0:
            continue

        pair = f"{asset}USDT"
        if asset == "USDT":
            price = 1.0
        elif asset in {"USD", "USDC", "BUSD", "FDUSD", "TUSD"}:
            price = 1.0
            pair = f"{asset}USDT"
        else:
            try:
                price = float(await binance_data.get_price(pair))
            except Exception:
                price = 0.0

        value_usdt = qty * price
        total_usdt_value += value_usdt
        rows.append(
            {
                "asset": asset,
                "free": round(free, 10),
                "locked": round(locked, 10),
                "quantity": round(qty, 10),
                "pair": pair,
                "price_usdt": round(price, 8),
                "value_usdt": round(value_usdt, 8),
            }
        )

    rows.sort(key=lambda x: x["value_usdt"], reverse=True)
    return {
        "ok": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": rows,
        "totals": {
            "estimated_usdt": round(total_usdt_value, 8),
            "usdt_free": round(float(usdt_free or 0.0), 8),
        },
    }


@router.get("/candle/analyze")
async def candle_analyze(symbol: str = "BTCUSDT", strategy_id: str = "sweep_bos_v1"):
    """Run the strategy on demand and return the signal (does NOT trade)."""
    if strategy_id not in candle_strat.SUPPORTED_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"unsupported strategy_id: {strategy_id}")
    signal = await candle_strat.analyze_symbol_with_strategy(symbol, strategy_id)
    return {
        "symbol": symbol.upper(),
        "strategy_id": strategy_id,
        "direction": signal.direction,
        "confidence": signal.confidence,
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "rr_ratio": signal.rr_ratio,
        "htf_bias": signal.htf_bias,
        "setup_type": signal.setup_type,
        "reasoning": signal.reasoning,
        "meta": signal.meta,
    }


# ── Bot control: paper ───────────────────────────────────────────────

@router.post("/candle/paper/bot/start")
async def candle_paper_bot_start(body: CandleBotStartBody):
    if body.strategy_id not in candle_strat.SUPPORTED_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"unsupported strategy_id: {body.strategy_id}")
    await candle_paper_bot.start(strategy_id=body.strategy_id)
    return candle_paper_bot.status()


@router.post("/candle/paper/bot/stop")
async def candle_paper_bot_stop():
    await candle_paper_bot.stop()
    return candle_paper_bot.status()


@router.get("/candle/paper/bot/status")
async def candle_paper_bot_status(session: AsyncSession = Depends(get_session)):
    s = candle_paper_bot.status()
    res = await session.execute(
        select(CandleTrade).where(CandleTrade.status == "OPEN", CandleTrade.mode == "paper")
    )
    s["active_positions"] = len(list(res.scalars().all()))
    return s


# ── Bot control: live ────────────────────────────────────────────────

@router.post("/candle/live/bot/start")
async def candle_live_bot_start(body: CandleBotStartBody):
    if body.strategy_id not in candle_strat.SUPPORTED_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"unsupported strategy_id: {body.strategy_id}")
    await candle_live_bot.start(strategy_id=body.strategy_id)
    return candle_live_bot.status()


@router.post("/candle/live/bot/stop")
async def candle_live_bot_stop():
    await candle_live_bot.stop()
    return candle_live_bot.status()


@router.get("/candle/live/bot/status")
async def candle_live_bot_status(session: AsyncSession = Depends(get_session)):
    s = candle_live_bot.status()
    res = await session.execute(
        select(CandleTrade).where(CandleTrade.status == "OPEN", CandleTrade.mode == "live")
    )
    s["active_positions"] = len(list(res.scalars().all()))
    s["mode_guard"] = mode_guard.get("candle").to_dict()
    return s


# ── Wallet ───────────────────────────────────────────────────────────

async def _candle_wallet_response(mode: str, session: AsyncSession) -> dict:
    is_live = mode == "live"
    w = (await session.execute(select(CandleWallet).where(CandleWallet.id == 1))).scalar_one_or_none()
    if w is None:
        w = CandleWallet(
            id=1,
            paper_balance=settings.CANDLE_PAPER_STARTING_BALANCE,
            paper_starting_balance=settings.CANDLE_PAPER_STARTING_BALANCE,
        )
        session.add(w)
        await session.commit()
        w = (await session.execute(select(CandleWallet).where(CandleWallet.id == 1))).scalar_one()

    live_error: str | None = None
    live_balance = float(w.live_balance_usdt or 0.0)
    if is_live:
        if not (settings.BINANCE_API_KEY and settings.BINANCE_API_SECRET):
            live_error = "Binance API credentials not configured (BINANCE_API_KEY/SECRET)"
        else:
            usdt, _, err = await binance_live.get_account_balance_safe()
            if err is None:
                live_balance = usdt
                w.live_balance_usdt = round(usdt, 6)
                w.live_balance_updated_at = datetime.now(timezone.utc)
                await session.commit()
            else:
                live_error = err

    # Stats for the requested mode.
    if is_live:
        balance = live_balance
        total_pnl = float(w.live_total_pnl or 0)
        total_trades = int(w.live_total_trades or 0)
        wins = int(w.live_wins or 0)
        losses = int(w.live_losses or 0)
    else:
        balance = float(w.paper_balance or 0)
        total_pnl = float(w.paper_total_pnl or 0)
        total_trades = int(w.paper_total_trades or 0)
        wins = int(w.paper_wins or 0)
        losses = int(w.paper_losses or 0)
    win_rate = (wins / total_trades * 100) if total_trades else 0.0

    return {
        "mode": mode,
        "balance": round(balance, 6),
        "paper_starting_balance": round(float(w.paper_starting_balance or 0), 2),
        "live_balance_updated_at": (w.live_balance_updated_at.isoformat() if w.live_balance_updated_at else None),
        "live_error": live_error,
        "total_pnl": round(total_pnl, 6),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "symbols": [s.strip() for s in settings.CANDLE_SYMBOLS.split(",") if s.strip()],
        "primary_interval": settings.CANDLE_PRIMARY_INTERVAL,
        "htf_interval": settings.CANDLE_HTF_INTERVAL,
        "risk_per_trade_pct": settings.CANDLE_RISK_PER_TRADE_PCT,
        "rr_ratio": settings.CANDLE_MIN_RR_RATIO,
    }


@router.get("/candle/paper/wallet")
async def candle_paper_wallet(session: AsyncSession = Depends(get_session)):
    return await _candle_wallet_response("paper", session)


@router.get("/candle/live/wallet")
async def candle_live_wallet(session: AsyncSession = Depends(get_session)):
    return await _candle_wallet_response("live", session)


class CandlePaperResetBody(BaseModel):
    passcode: str
    starting_balance: float = 20.0
    clear_logs: bool = False


@router.post("/candle/paper/reset")
async def candle_paper_reset(body: CandlePaperResetBody, session: AsyncSession = Depends(get_session)):
    """Hard reset candle paper mode: stop bot, clear paper trades, set wallet."""
    if body.passcode != settings.LIVE_MODE_CONFIRM_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    if body.starting_balance <= 0:
        raise HTTPException(status_code=400, detail="starting_balance must be > 0")

    # Stop paper bot before data reset to avoid races.
    await candle_paper_bot.stop()

    # Clear paper trades.
    await session.execute(delete(CandleTrade).where(CandleTrade.mode == "paper"))

    # Reset wallet counters.
    w = (await session.execute(select(CandleWallet).where(CandleWallet.id == 1))).scalar_one_or_none()
    if w is None:
        w = CandleWallet(id=1)
        session.add(w)
    w.paper_starting_balance = round(float(body.starting_balance), 6)
    w.paper_balance = round(float(body.starting_balance), 6)
    w.paper_total_pnl = 0.0
    w.paper_total_trades = 0
    w.paper_wins = 0
    w.paper_losses = 0
    await session.commit()

    deleted_logs = 0
    if body.clear_logs:
        res = await session.execute(
            delete(BotLog).where(
                text("json_extract(metadata_json, '$.platform') = 'candle'"),
                text("json_extract(metadata_json, '$.bot_kind') = 'paper'"),
            )
        )
        await session.commit()
        deleted_logs = int(res.rowcount or 0)

    return {
        "ok": True,
        "starting_balance": round(float(body.starting_balance), 2),
        "deleted_paper_trades": "all",
        "deleted_paper_logs": deleted_logs,
        "status": candle_paper_bot.status(),
        "wallet": await _candle_wallet_response("paper", session),
    }


# ── Trades & logs ────────────────────────────────────────────────────

@router.get("/candle/trades")
async def candle_trades(
    mode: str = "paper",
    status: str = "all",  # open | closed | all
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    if mode not in {"paper", "live"}:
        raise HTTPException(status_code=400, detail="invalid mode")
    q = select(CandleTrade).where(CandleTrade.mode == mode).order_by(desc(CandleTrade.opened_at)).limit(min(limit, 500))
    if status == "open":
        q = select(CandleTrade).where(CandleTrade.mode == mode, CandleTrade.status == "OPEN").order_by(desc(CandleTrade.opened_at)).limit(min(limit, 500))
    elif status == "closed":
        q = select(CandleTrade).where(CandleTrade.mode == mode, CandleTrade.status.like("CLOSED%")).order_by(desc(CandleTrade.opened_at)).limit(min(limit, 500))
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": t.id,
            "symbol": t.symbol,
            "interval": t.interval,
            "direction": t.direction,
            "qty": t.qty,
            "notional_usd": t.notional_usd,
            "entry_price": t.entry_price,
            "stop_loss": t.stop_loss,
            "take_profit": t.take_profit,
            "current_price": t.current_price,
            "exit_price": t.exit_price,
            "pnl_usd": t.pnl_usd,
            "pnl_pct": t.pnl_pct,
            "status": t.status,
            "htf_bias": t.htf_bias,
            "setup_type": t.setup_type,
            "confidence": t.confidence,
            "reasoning": t.reasoning,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        }
        for t in rows
    ]


@router.get("/candle/logs")
async def candle_logs(
    mode: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
):
    res = await session.execute(
        select(BotLog).order_by(desc(BotLog.created_at)).limit(800)
    )
    rows = res.scalars().all()
    wanted_kind = None
    if mode and mode.lower() in {"paper", "live"}:
        wanted_kind = mode.lower()
    candle_rows = [
        r for r in rows
        if (r.metadata_json or {}).get("platform") == "candle"
        and (wanted_kind is None or (r.metadata_json or {}).get("bot_kind") == wanted_kind)
    ][:limit]
    return [
        {
            "id": r.id,
            "level": r.level,
            "message": r.message,
            "ts": r.created_at.isoformat() if r.created_at else None,
        }
        for r in candle_rows
    ]
