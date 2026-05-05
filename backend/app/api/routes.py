from __future__ import annotations

import asyncio
import time
import csv
import io
from datetime import datetime, timedelta, timezone
import httpx

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import knowledge
from app.agent.analyzer import analyze_market, check_claude_health, check_gemini_health
from app.core.config import settings
from app.core.db import get_session
from app.models.models import BotLog, PolyTrade, Trade
from app.services import executor as exec_svc
from app.services import poly_wallet, strategies, wallet
from app.services.bot import bot
from app.services.bot_polymarket import poly_bot
from app.services.events import bus
from app.services.intel import gather_market_intel
from app.services.kalshi import get_kalshi
from app.services.kalshi import Market
from app.services.mode_guard import mode_guard
from app.services.poly_live import get_live_balance, get_live_balance_error
from app.services.polymarket import get_polymarket
from app.services.wallet_reconcile import reconcile_kalshi_paper, reconcile_polymarket_paper
from app.services.canary_guard import check_kalshi_canary, check_polymarket_canary

router = APIRouter()
_MARKET_CLOSE_CACHE: dict[str, tuple[float, int | None]] = {}
_MARKET_CLOSE_CACHE_TTL_SECONDS = 30.0


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


@router.post("/maintenance/poly/reset")
async def maintenance_poly_reset(body: PolyResetBody, session: AsyncSession = Depends(get_session)):
    if body.passcode != settings.MAINTENANCE_PASSCODE:
        raise HTTPException(status_code=401, detail="invalid passcode")
    await poly_bot.stop()
    await session.execute(delete(PolyTrade))
    w = await poly_wallet.get_wallet(session)
    w.balance = round(max(body.balance, 0.0), 4)
    w.total_pnl = 0.0
    w.total_trades = 0
    w.wins = 0
    w.losses = 0
    await session.commit()
    await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
    await bus.publish("polymarket:trades:cleared", {"ok": True})
    return {"ok": True, "balance": w.balance, "trades_deleted": "all"}


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
    await poly_bot.start()
    return poly_bot.status()


@router.post("/polymarket/bot/stop")
async def polymarket_bot_stop():
    await poly_bot.stop()
    return poly_bot.status()


@router.get("/polymarket/bot/status")
async def polymarket_bot_status(session: AsyncSession = Depends(get_session)):
    current_mode = "live" if mode_guard.get("polymarket").mode == "live_armed" else "paper"
    res = await session.execute(
        select(PolyTrade).where(PolyTrade.status == "OPEN", PolyTrade.mode == current_mode)
    )
    active = len(list(res.scalars().all()))
    s = poly_bot.status()
    s["active_positions"] = active
    s["max_concurrent_positions"] = settings.POLYMARKET_MAX_OPEN_POSITIONS
    s["mode_guard"] = mode_guard.get("polymarket").to_dict()
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


@router.get("/mode/status")
async def mode_status():
    return mode_guard.snapshot()


@router.post("/mode/request-live")
async def mode_request_live(body: ModeRequestBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    result = mode_guard.request_live(platform)  # type: ignore[arg-type]
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "request failed"))
    return result


@router.post("/mode/confirm-live")
async def mode_confirm_live(body: ModeConfirmBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    result = mode_guard.confirm_live(platform, body.passcode, body.limits)  # type: ignore[arg-type]
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "confirm failed"))
    return result


@router.post("/mode/set-paper")
async def mode_set_paper(body: ModeSetPaperBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    return mode_guard.set_paper(platform)  # type: ignore[arg-type]


@router.post("/mode/kill-switch")
async def mode_kill_switch(body: KillSwitchBody):
    platform = body.platform.lower()
    if platform not in {"kalshi", "polymarket"}:
        raise HTTPException(status_code=400, detail="invalid platform")
    return mode_guard.set_kill_switch(platform, body.enabled)  # type: ignore[arg-type]


@router.get("/polymarket/wallet")
async def polymarket_wallet_get(session: AsyncSession = Depends(get_session)):
    is_live = mode_guard.get("polymarket").mode == "live_armed"
    current_mode = "live" if is_live else "paper"
    live_error = None

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
    else:
        w = await poly_wallet.get_wallet(session)
        balance = w.balance

    closed_q = select(PolyTrade).where(
        PolyTrade.mode == current_mode,
        PolyTrade.status.like("CLOSED%"),
    )
    closed = (await session.execute(closed_q)).scalars().all()
    total_pnl = float(sum((t.pnl or 0.0) for t in closed))
    wins = sum(1 for t in closed if (t.pnl or 0.0) > 0)
    losses = sum(1 for t in closed if (t.pnl or 0.0) < 0)
    total_trades = len(closed)
    win_rate = (wins / total_trades * 100) if total_trades else 0.0

    return {
        "balance": round(balance, 4),
        "total_pnl": round(total_pnl, 4),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "live_error": live_error,
    }


@router.get("/polymarket/live/health")
async def polymarket_live_health():
    """Quick connectivity check for the live CLOB client."""
    from app.services.poly_live import get_live_client, get_live_balance_error
    client = get_live_client()
    if client is None:
        return {"ok": False, "error": "ClobClient not initialised — check POLYMARKET_PRIVATE_KEY"}
    balance = await get_live_balance()
    err = get_live_balance_error()
    return {
        "ok": not bool(err),
        "balance": balance,
        "error": err or None,
        "funder": getattr(getattr(client, "builder", None), "funder", None),
        "sig_type": getattr(getattr(client, "builder", None), "signature_type", None),
    }


@router.get("/polymarket/trades")
async def polymarket_trades(
    status: str = "open",
    limit: int = 50,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
):
    current_mode = "live" if mode_guard.get("polymarket").mode == "live_armed" else "paper"
    q = select(PolyTrade).where(PolyTrade.mode == current_mode)
    if status == "open":
        q = q.where(PolyTrade.status == "OPEN")
    elif status == "closed":
        q = q.where(PolyTrade.status.like("CLOSED%"))
    q = q.order_by(desc(PolyTrade.opened_at)).offset(max(page - 1, 0) * limit).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    return [_poly_trade_dict(t) for t in rows]


@router.get("/polymarket/trades/summary")
async def polymarket_trades_summary(session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    start_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    current_mode = "live" if mode_guard.get("polymarket").mode == "live_armed" else "paper"
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
