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
from app.services.mode_guard import mode_guard
from app.services.canary_guard import check_polymarket_canary
from app.services.polymarket import get_polymarket
from app.services import poly_wallet
from app.services.poly_analyzer import analyze_polymarket
from app.services.risk_engine import check_polymarket_entry_risk

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
        if settings.POLYMARKET_PREFER_MICRO_UPDOWN:
            micro = [
                m for m in markets
                if self._is_micro_updown(m)
                and min_t <= m.close_time_seconds <= max_t
                and m.close_time_seconds <= max(
                    min_t, settings.POLYMARKET_MICRO_MAX_TIME_TO_CLOSE_SECONDS
                )
                and m.volume >= settings.POLYMARKET_MICRO_MIN_VOLUME
            ]
            if micro:
                candidates = micro
        self.last_candidate_count = len(candidates)
        if not candidates:
            return

        candidates.sort(key=lambda m: m.volume, reverse=True)
        self.state = "ANALYZING"
        async with SessionLocal() as s:
            await self._manage_positions(s, poly)
            open_rows = (
                await s.execute(select(PolyTrade).where(PolyTrade.status == "OPEN"))
            ).scalars().all()
            if len(open_rows) >= settings.POLYMARKET_MAX_OPEN_POSITIONS:
                return

            open_market_ids = {t.market_id for t in open_rows}
            wallet = await poly_wallet.get_wallet(s)

            opened_this_tick = 0
            max_per_tick = max(1, getattr(settings, "POLYMARKET_MAX_OPENS_PER_TICK", 5))

            for m in candidates[:25]:
                if m.id in open_market_ids:
                    continue
                if len(open_rows) + opened_this_tick >= settings.POLYMARKET_MAX_OPEN_POSITIONS:
                    break

                # AI + candle + cheatsheet decision.
                analysis = await analyze_polymarket(m)
                action = (analysis.action or "SKIP").upper()
                if action not in {"BUY_YES", "BUY_NO"}:
                    continue
                if analysis.score < settings.POLYMARKET_MIN_SCORE:
                    continue

                side = "YES" if action == "BUY_YES" else "NO"
                entry = m.yes_price if side == "YES" else m.no_price

                risk = await check_polymarket_entry_risk(
                    s, m, score=analysis.score, open_positions=len(open_rows) + opened_this_tick
                )
                if not risk.allow:
                    await self._log("INFO", f"polymarket risk blocked {m.id}: {risk.reason}", risk=risk.meta)
                    continue
                amount = settings.POLYMARKET_TRADE_AMOUNT_USD
                if wallet.balance < amount:
                    break

                self.state = "EXECUTING"
                can_open, reason = mode_guard.can_open_new_trade("polymarket")
                if not can_open:
                    await self._log("INFO", f"polymarket mode guard blocked open: {reason}")
                    continue
                mode = mode_guard.get("polymarket").mode
                canary_ok, canary_reason, canary_meta = await check_polymarket_canary(
                    s, mode=mode, order_usd=amount
                )
                if not canary_ok:
                    await self._log("INFO", f"polymarket canary blocked open: {canary_reason}", canary=canary_meta)
                    continue
                await poly_wallet.debit(s, amount)
                reasoning_blob = (
                    f"asset={analysis.asset} interval={analysis.interval} "
                    f"conf={analysis.confidence:.2f} {analysis.reasoning}"
                )
                t = PolyTrade(
                    market_id=m.id,
                    market_title=m.title,
                    direction=side,
                    amount=amount,
                    entry_price=entry,
                    current_price=entry,
                    status="OPEN",
                    agent_score=analysis.score,
                    reasoning=reasoning_blob[:280],
                )
                s.add(t)
                await s.commit()
                opened_this_tick += 1
                self.trades_today += 1
                await bus.publish(
                    "polymarket:trade:opened",
                    {
                        "id": t.id,
                        "market_id": t.market_id,
                        "direction": t.direction,
                        "entry_price": t.entry_price,
                        "agent_score": t.agent_score,
                        "asset": analysis.asset,
                        "interval": analysis.interval,
                    },
                )
                await bus.publish("polymarket:wallet:updated", {"balance": wallet.balance})
                await self._log(
                    "INFO",
                    f"polymarket opened {side} {m.id} @ {entry} ({analysis.asset}/{analysis.interval}, score={analysis.score})",
                )
                # Refresh wallet snapshot so next iteration sees debited balance.
                wallet = await poly_wallet.get_wallet(s)
                if opened_this_tick >= max_per_tick:
                    break

    @staticmethod
    def _is_micro_updown(m) -> bool:
        title = (m.title or "").lower()
        slug = str((m.raw or {}).get("slug") or "").lower()
        event_slug = str((((m.raw or {}).get("events") or [{}])[0].get("slug") or "")).lower()
        hay = f"{title} {slug} {event_slug}"
        if not (("up or down" in hay) or ("updown" in hay)):
            return False
        # Focus only on short windows requested by user: 5m / 15m.
        return any(tok in hay for tok in ("5m", "15m", "5 min", "15 min"))

    async def _manage_positions(self, s, poly) -> None:
        open_rows = (await s.execute(select(PolyTrade).where(PolyTrade.status == "OPEN"))).scalars().all()
        if not open_rows:
            return
            
        for t in open_rows:
            try:
                res = await poly._http.get(f"https://gamma-api.polymarket.com/markets/{t.market_id}")
                if res.status_code != 200:
                    continue
                data = res.json()
                closed = data.get("closed", False)
                active = data.get("active", True)
                
                if (not closed) and active:
                    raw_live = data.get("outcomePrices")
                    if isinstance(raw_live, str):
                        import json as _json_live
                        try:
                            raw_live = _json_live.loads(raw_live)
                        except Exception:
                            raw_live = None
                    if isinstance(raw_live, list) and len(raw_live) >= 2:
                        try:
                            cur_yes = float(raw_live[0])
                            cur_no = float(raw_live[1])
                        except (TypeError, ValueError):
                            cur_yes = cur_no = None
                        if cur_yes is not None and t.entry_price and t.entry_price > 0:
                            cur = cur_yes if t.direction == "YES" else cur_no
                            if cur >= t.entry_price * 1.15:
                                contracts = t.amount / max(t.entry_price, 0.01)
                                payout = contracts * cur
                                pnl = round(payout - t.amount, 4)
                                t.status = "CLOSED_TAKE_PROFIT"
                                t.exit_price = cur
                                t.pnl = pnl
                                t.closed_at = datetime.now(timezone.utc)
                                await poly_wallet.credit(s, payout)
                                await poly_wallet.record_close(s, pnl, pnl > 0)
                                await s.commit()
                                await self._log(
                                    "INFO",
                                    f"polymarket TP+15% closed {t.id} entry={t.entry_price:.3f} exit={cur:.3f} pnl={pnl:.3f}",
                                )
                                await bus.publish("polymarket:trade:closed", {"id": t.id, "pnl": pnl})
                                w = await poly_wallet.get_wallet(s)
                                await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
                                continue

                if closed or not active:
                    raw_outcomes = data.get("outcomePrices")
                    if isinstance(raw_outcomes, str):
                        import json
                        try:
                            raw_outcomes = json.loads(raw_outcomes)
                        except Exception:
                            pass

                    if isinstance(raw_outcomes, list) and len(raw_outcomes) >= 2:
                        val_yes = str(raw_outcomes[0]).strip()
                        val_no = str(raw_outcomes[1]).strip()
                        
                        if val_yes in ("1", "1.0") or val_no in ("1", "1.0"):
                            yes_won = (val_yes in ("1", "1.0"))
                            exit_price = 1.0 if (t.direction == "YES" and yes_won) or (t.direction == "NO" and not yes_won) else 0.0
                            
                            contracts = t.amount / max(t.entry_price, 0.01)
                            if exit_price == 1.0:
                                pnl = (contracts * 1.0) - t.amount
                                win = True
                                status = "CLOSED_WIN"
                            else:
                                pnl = -t.amount
                                win = False
                                status = "CLOSED_LOSS"
                                
                            t.status = status
                            t.exit_price = exit_price
                            t.pnl = round(pnl, 4)
                            t.closed_at = datetime.now(timezone.utc)
                            
                            await poly_wallet.credit(s, t.amount + t.pnl)
                            await poly_wallet.record_close(s, t.pnl, win)
                            await s.commit()
                            
                            await self._log("INFO", f"polymarket closed {t.id} {status} pnl={t.pnl:.2f}")
                            await bus.publish("polymarket:trade:closed", {"id": t.id, "pnl": t.pnl})
                            w = await poly_wallet.get_wallet(s)
                            await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
            except Exception as e:
                await self._log("ERROR", f"polymarket manage position {t.id} failed: {e}")



poly_bot = PolymarketBot()
