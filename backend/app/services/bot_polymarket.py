"""Phase-1 paper bot for Polymarket (separate from Kalshi bot)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.models import BotLog, PolyTrade
from app.services.events import bus
from app.services.polymarket import get_polymarket
from app.services import poly_wallet

State = Literal["IDLE", "SCANNING", "ANALYZING", "EXECUTING", "STOPPED"]


class PolymarketBot:
    def __init__(self) -> None:
        self.state: State = "STOPPED"
        self.started_at: datetime | None = None
        self.trades_today = 0
        self.scanned_markets_today = 0
        self.last_scan_count = 0
        self.last_candidate_count = 0
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
        await self._log("INFO", "polymarket bot started")
        await bus.publish("polymarket:bot:status", self.status())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self._task = None
        self.state = "STOPPED"
        await self._log("INFO", "polymarket bot stopped")
        await bus.publish("polymarket:bot:status", self.status())

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
            s.add(BotLog(level=level, message=message, metadata_json={"platform": "polymarket", **(meta or {})}))
            await s.commit()
        await bus.publish(
            "agent:log",
            {"level": level, "message": message, "metadata": {"platform": "polymarket", **meta}, "ts": datetime.now(timezone.utc).isoformat()},
        )

    async def _loop(self) -> None:
        poly = get_polymarket()
        while not self._stop_event.is_set():
            try:
                await self._tick(poly)
            except Exception as e:
                await self._log("ERROR", f"polymarket tick failed: {e}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=settings.POLYMARKET_SCAN_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self, poly) -> None:
        self.state = "SCANNING"
        markets = await poly.get_markets(limit=300)
        self.last_scan_count = len(markets)
        self.scanned_markets_today += len(markets)

        min_t = max(60, settings.POLYMARKET_MIN_TIME_TO_CLOSE_SECONDS)
        max_t = max(min_t, settings.POLYMARKET_MAX_TIME_TO_CLOSE_SECONDS)
        candidates = [
            m for m in markets
            if min_t <= m.close_time_seconds <= max_t and m.volume >= 300
        ]
        self.last_candidate_count = len(candidates)
        if not candidates:
            return

        candidates.sort(key=lambda m: m.volume, reverse=True)
        self.state = "ANALYZING"
        async with SessionLocal() as s:
            open_rows = (
                await s.execute(select(PolyTrade).where(PolyTrade.status == "OPEN"))
            ).scalars().all()
            if len(open_rows) >= settings.POLYMARKET_MAX_OPEN_POSITIONS:
                return

            open_market_ids = {t.market_id for t in open_rows}
            wallet = await poly_wallet.get_wallet(s)

            for m in candidates[:25]:
                if m.id in open_market_ids:
                    continue
                side = "YES" if m.yes_price <= m.no_price else "NO"
                entry = m.yes_price if side == "YES" else m.no_price
                edge = max(0.0, 0.5 - entry)
                score = int(round(min(99, 52 + edge * 90 + min(m.volume / 4000.0, 1.0) * 22)))
                if score < settings.POLYMARKET_MIN_SCORE:
                    continue
                amount = settings.POLYMARKET_TRADE_AMOUNT_USD
                if wallet.balance < amount:
                    break

                self.state = "EXECUTING"
                await poly_wallet.debit(s, amount)
                t = PolyTrade(
                    market_id=m.id,
                    market_title=m.title,
                    direction=side,
                    amount=amount,
                    entry_price=entry,
                    current_price=entry,
                    status="OPEN",
                    agent_score=score,
                    reasoning=f"phase1 paper signal; side={side}; edge={edge:.2f}; vol={m.volume:.0f}",
                )
                s.add(t)
                await s.commit()
                self.trades_today += 1
                await bus.publish(
                    "polymarket:trade:opened",
                    {"id": t.id, "market_id": t.market_id, "direction": t.direction, "entry_price": t.entry_price, "agent_score": t.agent_score},
                )
                await bus.publish("polymarket:wallet:updated", {"balance": wallet.balance})
                await self._log("INFO", f"polymarket opened {side} {m.id} @ {entry}")
                # One new trade per tick to keep phase-1 controlled.
                break


poly_bot = PolymarketBot()

