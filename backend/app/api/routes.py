from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import knowledge
from app.agent.analyzer import analyze_market, check_claude_health, check_gemini_health
from app.core.config import settings
from app.core.db import get_session
from app.models.models import BotLog, Trade
from app.services import executor as exec_svc
from app.services import strategies, wallet
from app.services.bot import bot
from app.services.events import bus
from app.services.kalshi import get_kalshi

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


@router.get("/trades/{trade_id}")
async def trade_get(trade_id: str, session: AsyncSession = Depends(get_session)):
    res = await session.execute(select(Trade).where(Trade.id == trade_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="trade not found")
    return _trade_dict(t)


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


async def _time_to_close_seconds(kalshi, market_id: str) -> int | None:
    now = time.time()
    cached = _MARKET_CLOSE_CACHE.get(market_id)
    if cached and now - cached[0] < _MARKET_CLOSE_CACHE_TTL_SECONDS:
        return cached[1]
    market = await kalshi.get_market(market_id)
    value = market.close_time_seconds if market is not None else None
    _MARKET_CLOSE_CACHE[market_id] = (now, value)
    return value
