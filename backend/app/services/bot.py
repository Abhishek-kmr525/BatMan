"""Bot state machine + scanning loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from app.agent.analyzer import analyze_market
from app.core.config import settings
from app.core.db import SessionLocal
from app.models.models import BotLog
from app.services import executor, strategies
from app.services.events import bus
from app.services.kalshi import get_kalshi
from app.services.mode_guard import mode_guard
from app.services.canary_guard import check_kalshi_canary
from app.services.risk_engine import check_kalshi_entry_risk

State = Literal["IDLE", "SCANNING", "ANALYZING", "EXECUTING", "MONITORING", "STOPPED"]


class Bot:
    def __init__(self) -> None:
        self.state: State = "STOPPED"
        self.started_at: datetime | None = None
        self.trades_today: int = 0
        self.scanned_markets_today: int = 0
        self.last_scan_count: int = 0
        self.last_candidate_count: int = 0
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self.started_at = datetime.now(timezone.utc)
        self.state = "SCANNING"
        self.trades_today = 0
        self.scanned_markets_today = 0
        self.last_scan_count = 0
        self.last_candidate_count = 0
        self._task = asyncio.create_task(self._loop())
        await self._log("INFO", "bot started")
        await bus.publish("bot:status", self.status())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self._task = None
        self.state = "STOPPED"
        await self._log("INFO", "bot stopped")
        await bus.publish("bot:status", self.status())

    def status(self) -> dict:
        uptime = 0
        if self.started_at and self.running:
            uptime = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        return {
            "status": "running" if self.running else "stopped",
            "state": self.state,
            "uptime_seconds": uptime,
            "trades_today": self.trades_today,
            "scanned_markets_today": self.scanned_markets_today,
            "last_scan_count": self.last_scan_count,
            "last_candidate_count": self.last_candidate_count,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }

    async def _log(self, level: str, message: str, **meta) -> None:
        async with SessionLocal() as s:
            s.add(BotLog(level=level, message=message, metadata_json=meta or None))
            await s.commit()
        await bus.publish(
            "agent:log",
            {"level": level, "message": message, "metadata": meta, "ts": datetime.now(timezone.utc).isoformat()},
        )

    async def _loop(self) -> None:
        kalshi = get_kalshi()
        while not self._stop_event.is_set():
            try:
                await self._tick(kalshi)
            except Exception as e:
                await self._log("ERROR", f"tick failed: {e}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=settings.BOT_SCAN_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self, kalshi) -> None:
        # 1. Monitor open positions for exits
        self.state = "MONITORING"
        async with SessionLocal() as s:
            open_trades = await executor.list_open(s)
            for t in open_trades:
                closed = await executor.evaluate_exit(s, kalshi, t)
                if closed is not None:
                    await s.commit()
                    await bus.publish(
                        "trade:closed",
                        {
                            "id": closed.id,
                            "market_id": closed.market_id,
                            "pnl": closed.pnl,
                            "status": closed.status,
                        },
                    )
                    await self._log(
                        "INFO",
                        f"closed {closed.market_id} pnl={closed.pnl}",
                        status=closed.status,
                    )

        # 2. Scan markets for new entries
        self.state = "SCANNING"
        # Pull a pool of liquid markets (paginates real Kalshi until found).
        # Fast path: parallel fetch of known-liquid series. Falls back to
        # full pagination only if zero series respond (e.g. Kalshi changed
        # series naming).
        scan_limit = 600 if settings.QUICK_EXPIRY_ALWAYS_ON else 120
        markets = await kalshi.get_liquid_markets(limit=scan_limit, min_volume=10)
        if not markets:
            markets = await kalshi.get_markets(limit=scan_limit, min_volume=0, max_pages=80)
            markets = [m for m in markets if m.volume >= 10]
            markets.sort(key=lambda m: m.volume, reverse=True)
            markets = markets[:scan_limit]
        self.last_scan_count = len(markets)
        self.scanned_markets_today += len(markets)
        await bus.publish("market:scan", {"count": len(markets)})

        active_list = strategies.get_active_list()
        if settings.QUICK_EXPIRY_ALWAYS_ON:
            min_close = max(60, settings.QUICK_EXPIRY_MIN_SECONDS)
            max_close = max(min_close, settings.QUICK_EXPIRY_MAX_SECONDS)
            candidates = [m for m in markets if min_close <= m.close_time_seconds <= max_close]
        else:
            candidates = [m for m in markets if m.close_time_seconds > 5 * 60]
        if active_list:
            # Keep markets matched by ANY active strategy.
            candidates = [m for m in candidates if any(s.filter(m) for s in active_list)]
        self.last_candidate_count = len(candidates)
        # Always sort by volume — most liquid first.
        candidates.sort(key=lambda m: m.volume, reverse=True)

        async with SessionLocal() as s:
            opens = await executor.open_count(s)
            slots = max(0, settings.MAX_CONCURRENT_POSITIONS - opens)

        analyzed = 0
        for market in candidates:
            if self._stop_event.is_set():
                break
            if slots <= 0:
                break
            if analyzed >= min(max(slots * 2, 5), settings.MAX_ANALYSES_PER_TICK):
                break
            self.state = "ANALYZING"
            try:
                analysis = await analyze_market(market)
            except Exception as e:
                await self._log("WARN", f"analyze failed for {market.ticker}: {e}")
                continue
            analyzed += 1

            # Per-market strategy resolution: first active strategy whose
            # filter matches this market wins. None when no strategies active.
            claimed = strategies.claim(market) if active_list else None
            min_score = claimed.min_score if claimed else settings.MIN_TRADE_SCORE
            strategy_id: str | None = claimed.id if claimed else None

            if analysis.score < min_score or analysis.action == "SKIP":
                continue

            self.state = "EXECUTING"
            async with SessionLocal() as s:
                can_open, reason = mode_guard.can_open_new_trade("kalshi")
                if not can_open:
                    await self._log("INFO", f"kalshi mode guard blocked open: {reason}")
                    continue
                mode = mode_guard.get("kalshi").mode
                canary_ok, canary_reason, canary_meta = await check_kalshi_canary(
                    s, mode=mode, order_usd=settings.TRADE_AMOUNT_USD
                )
                if not canary_ok:
                    await self._log(
                        "INFO",
                        f"kalshi canary blocked open: {canary_reason}",
                        canary=canary_meta,
                    )
                    continue
                open_positions = await executor.open_count(s)
                risk = await check_kalshi_entry_risk(
                    s, market, analysis, open_positions=open_positions
                )
                if not risk.allow:
                    await self._log(
                        "INFO",
                        f"risk blocked {market.ticker}: {risk.reason}",
                        risk=risk.meta,
                    )
                    continue
                trade = await executor.open_trade(
                    s, kalshi, market, analysis,
                    min_score=min_score,
                    strategy_id=strategy_id,
                    source="bot",
                )
                if trade is not None:
                    await s.commit()
                    self.trades_today += 1
                    await bus.publish(
                        "trade:opened",
                        {
                            "id": trade.id,
                            "market_id": trade.market_id,
                            "market_title": trade.market_title,
                            "direction": trade.direction,
                            "entry_price": trade.entry_price,
                            "agent_score": trade.agent_score,
                        },
                    )
                    await self._log(
                        "INFO",
                        f"opened {trade.direction} {trade.market_id} @ {trade.entry_price}",
                        score=trade.agent_score,
                    )


bot = Bot()
