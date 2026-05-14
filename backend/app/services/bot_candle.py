"""Candle Bot — runs the candle strategy on a loop for crypto pairs.

Paper mode: simulates fills using Binance's last price; tracks PnL in a
  per-bot wallet table (CandleWallet.paper_*).
Live mode:  executes MARKET orders on Binance Spot using BINANCE_API_KEY.
            Reads real USDT balance from the account on every tick.

The strategy logic lives in candle_strategy.py. This file is the runner.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.models import BotLog, CandleTrade, CandleWallet
from app.services import binance_data, binance_live, candle_strategy
from app.services.events import bus
from app.services.mode_guard import mode_guard

BotKind = Literal["paper", "live"]
State = Literal["IDLE", "SCANNING", "ANALYZING", "EXECUTING", "STOPPED"]


class CandleBot:
    """One scanner loop per kind (paper or live)."""

    # Per-symbol cooldown after entry to prevent re-stacking on same candle.
    _SYMBOL_COOLDOWN_SECS = 600  # 10 minutes

    def __init__(self, *, bot_kind: BotKind = "paper") -> None:
        self.bot_kind: BotKind = bot_kind
        self.trade_mode: str = bot_kind  # "paper" | "live"
        self.is_live: bool = bot_kind == "live"
        self.state: State = "STOPPED"
        self.started_at: datetime | None = None
        self.trades_today = 0
        self.last_scan_count = 0
        self.last_signal_count = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._recent_entries: dict[str, float] = {}  # symbol → unix epoch
        self._consecutive_losses = 0
        self._loss_cooldown_until: float = 0
        self._daily_loss_total: float = 0.0
        self._daily_loss_day: str = ""
        self.strategy_id: str = "sweep_bos_v1"

    # ─────────────────── Lifecycle ───────────────────────────────

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, strategy_id: str = "sweep_bos_v1") -> None:
        if self.running:
            return
        self.strategy_id = strategy_id
        self._stop.clear()
        self.started_at = datetime.now(timezone.utc)
        self.state = "SCANNING"
        self.trades_today = 0
        self._task = asyncio.create_task(self._loop())
        await self._log("INFO", f"candle bot started ({self.bot_kind}) strategy={self.strategy_id}")
        await bus.publish("candle:bot:status", self.status())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self._task = None
        self.state = "STOPPED"
        await self._log("INFO", f"candle bot stopped ({self.bot_kind})")
        await bus.publish("candle:bot:status", self.status())

    def status(self) -> dict:
        uptime = 0
        if self.started_at and self.running:
            uptime = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        return {
            "bot_kind": self.bot_kind,
            "status": "running" if self.running else "stopped",
            "state": self.state,
            "uptime_seconds": uptime,
            "trades_today": self.trades_today,
            "last_scan_count": self.last_scan_count,
            "last_signal_count": self.last_signal_count,
            "consecutive_losses": self._consecutive_losses,
            "daily_loss_usd": round(self._daily_loss_total, 4),
            "in_cooldown": time.time() < self._loss_cooldown_until,
            "cooldown_until_epoch": self._loss_cooldown_until if time.time() < self._loss_cooldown_until else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "strategy_id": self.strategy_id,
        }

    # ─────────────────── Logging ─────────────────────────────────

    async def _log(self, level: str, message: str, **meta) -> None:
        payload = {"platform": "candle", "bot_kind": self.bot_kind, **(meta or {})}
        for attempt in range(3):
            try:
                async with SessionLocal() as s:
                    s.add(BotLog(level=level, message=message, metadata_json=payload))
                    await s.commit()
                break
            except OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.12 * (attempt + 1))
                    continue
                break
            except Exception:
                break
        await bus.publish("agent:log", {
            "level": level,
            "message": message,
            "metadata": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    # ─────────────────── Main loop ───────────────────────────────

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                await self._log("ERROR", f"candle tick failed: {e}")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=settings.CANDLE_SCAN_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        # Live mode requires CANDLE_LIVE_ENABLED and live-armed mode_guard.
        if self.is_live:
            if not settings.CANDLE_LIVE_ENABLED:
                self.state = "IDLE"
                return
            mg = mode_guard.get("candle")
            if mg.mode != "live_armed" or mg.kill_switch:
                self.state = "IDLE"
                return

        # Refresh live USDT balance if live.
        live_balance = None
        if self.is_live:
            usdt, _, err = await binance_live.get_account_balance_safe()
            if err is None:
                live_balance = usdt
                await self._update_wallet_live_balance(usdt)

        # Daily loss tracking — reset counter on day rollover.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_loss_day:
            self._daily_loss_day = today
            self._daily_loss_total = 0.0

        # Daily loss cutoff.
        if self._daily_loss_total >= settings.CANDLE_MAX_DAILY_LOSS_USD:
            if time.time() < self._loss_cooldown_until:
                self.state = "IDLE"
                return
            else:
                # Reset after cooldown expires.
                self._daily_loss_total = 0.0

        # Manage existing open positions first.
        await self._manage_positions()

        # Check open position count cap.
        async with SessionLocal() as s:
            open_q = select(CandleTrade).where(
                CandleTrade.status == "OPEN",
                CandleTrade.mode == self.trade_mode,
            )
            open_rows = (await s.execute(open_q)).scalars().all()
        if len(open_rows) >= settings.CANDLE_MAX_OPEN_POSITIONS:
            return

        # Per-symbol cooldown filter.
        symbols = [s.strip().upper() for s in settings.CANDLE_SYMBOLS.split(",") if s.strip()]
        open_symbols = {t.symbol for t in open_rows}
        candidates: list[str] = []
        for sym in symbols:
            if sym in open_symbols:
                continue
            last_entry = self._recent_entries.get(sym, 0)
            if time.time() - last_entry < self._SYMBOL_COOLDOWN_SECS:
                continue
            candidates.append(sym)

        self.last_scan_count = len(symbols)
        if not candidates:
            return

        # Loss-cooldown check.
        if time.time() < self._loss_cooldown_until:
            return

        self.state = "ANALYZING"
        signals_found = 0
        for sym in candidates:
            try:
                signal = await asyncio.wait_for(
                    candle_strategy.analyze_symbol_with_strategy(sym, self.strategy_id), timeout=12
                )
            except asyncio.TimeoutError:
                await self._log("ERROR", f"strategy analyze timeout {sym}")
                continue

            await self._log("INFO", f"scan {sym}: {signal.direction} conf={signal.confidence:.2f} | {signal.reasoning[:90]}")

            if signal.direction == "SKIP":
                continue
            signals_found += 1

            await self._open_position(sym, signal, live_balance=live_balance)
            # Only open one new position per tick to throttle.
            break

        self.last_signal_count = signals_found

    # ─────────────────── Open position ───────────────────────────

    async def _open_position(self, symbol: str, signal, live_balance: float | None) -> None:
        self.state = "EXECUTING"
        # Determine equity for risk sizing.
        async with SessionLocal() as s:
            wallet = await self._get_wallet(s)
            equity = (
                wallet.live_balance_usdt if self.is_live else wallet.paper_balance
            )
        if equity <= 0:
            await self._log("INFO", f"skip {symbol}: zero equity ({equity})")
            return

        # Position size from risk per trade & SL distance.
        risk_usd = equity * settings.CANDLE_RISK_PER_TRADE_PCT
        if signal.direction == "LONG":
            stop_distance = signal.entry_price - signal.stop_loss
        else:
            stop_distance = signal.stop_loss - signal.entry_price
        if stop_distance <= 0:
            await self._log("INFO", f"skip {symbol}: invalid stop distance {stop_distance}")
            return
        # qty (base asset) = risk_usd / stop_distance
        qty = risk_usd / stop_distance
        notional_usd = qty * signal.entry_price

        # Enforce capital availability in both modes.
        if notional_usd > equity:
            await self._log("INFO", f"skip {symbol}: notional ${notional_usd:.2f} > equity ${equity:.2f}")
            return

        # Live mode: enforce exchange filters.
        if self.is_live:
            filters = await binance_live.get_symbol_filters(symbol)
            min_notional = filters.get("minNotional", 5.0)
            step = filters.get("stepSize", 0.0001)
            if notional_usd < min_notional:
                # Scale UP to min notional (will exceed our risk target — log).
                notional_usd = max(min_notional + 0.01, notional_usd)
                qty = notional_usd / signal.entry_price
            # Round qty DOWN to step size.
            n = int(qty / step) if step > 0 else 0
            qty = round(n * step, 10)
            notional_usd = qty * signal.entry_price
            if notional_usd < min_notional:
                await self._log("INFO", f"skip {symbol}: cannot meet min notional {min_notional}")
                return
            if notional_usd > equity:
                await self._log("INFO", f"skip {symbol}: rounded notional ${notional_usd:.2f} > equity ${equity:.2f}")
                return

        # Place order (or simulate).
        entry_order_id = None
        executed_price = signal.entry_price
        if self.is_live:
            if signal.direction == "LONG":
                res = await binance_live.place_market_buy(symbol, quote_usd=notional_usd)
            else:
                # Spot bot: SHORT not supported on spot — skip.
                await self._log("INFO", f"skip {symbol}: SHORT not supported on Binance Spot")
                return
            if not res.get("ok"):
                await self._log("ERROR", f"live entry failed {symbol}: {res.get('error')}")
                return
            entry_order_id = str(res.get("orderId"))
            executed_price = res.get("executed_price") or signal.entry_price
            qty = res.get("executed_qty") or qty
            notional_usd = res.get("cum_quote_usd") or notional_usd

        # Recompute SL/TP from actual fill price.
        if signal.direction == "LONG":
            sl = round(executed_price - stop_distance, 8)
            tp = round(executed_price + stop_distance * signal.rr_ratio, 8)
        else:
            sl = round(executed_price + stop_distance, 8)
            tp = round(executed_price - stop_distance * signal.rr_ratio, 8)

        async with SessionLocal() as s:
            t = CandleTrade(
                symbol=symbol,
                interval=settings.CANDLE_PRIMARY_INTERVAL,
                direction=signal.direction,
                qty=qty,
                notional_usd=round(notional_usd, 4),
                entry_price=executed_price,
                stop_loss=sl,
                take_profit=tp,
                current_price=executed_price,
                htf_bias=signal.htf_bias,
                setup_type=signal.setup_type,
                confidence=signal.confidence,
                rr_target=signal.rr_ratio,
                reasoning=signal.reasoning[:280],
                entry_order_id=entry_order_id,
                mode=self.trade_mode,
            )
            s.add(t)
            # Paper: deduct notional from balance.
            if not self.is_live:
                wallet = await self._get_wallet(s)
                wallet.paper_balance = round(wallet.paper_balance - notional_usd, 6)
            await s.commit()
            trade_id = t.id

        self._recent_entries[symbol] = time.time()
        self.trades_today += 1
        await self._log("INFO",
            f"OPENED {signal.direction} {symbol} qty={qty:.6f} @{executed_price:.4f} "
            f"SL={sl:.4f} TP={tp:.4f} (RR={signal.rr_ratio:.2f})"
        )
        await bus.publish("candle:trade:opened", {"id": trade_id, "symbol": symbol})

    # ─────────────────── Manage open positions ───────────────────

    async def _manage_positions(self) -> None:
        async with SessionLocal() as s:
            q = select(CandleTrade).where(
                CandleTrade.status == "OPEN",
                CandleTrade.mode == self.trade_mode,
            )
            rows = (await s.execute(q)).scalars().all()
        if not rows:
            return

        for t in rows:
            try:
                price = await binance_data.get_price(t.symbol)
                if price is None or price <= 0:
                    continue
                hit_sl = (
                    (t.direction == "LONG" and price <= t.stop_loss)
                    or (t.direction == "SHORT" and price >= t.stop_loss)
                )
                hit_tp = (
                    (t.direction == "LONG" and price >= t.take_profit)
                    or (t.direction == "SHORT" and price <= t.take_profit)
                )
                if not (hit_sl or hit_tp):
                    # Just refresh current_price.
                    async with SessionLocal() as s:
                        live_t = await s.get(CandleTrade, t.id)
                        if live_t:
                            live_t.current_price = price
                            await s.commit()
                    continue

                exit_price = price
                close_status = "CLOSED_STOP_LOSS" if hit_sl else "CLOSED_TAKE_PROFIT"
                exit_order_id = None

                # Live: sell base asset back to USDT.
                if self.is_live and t.direction == "LONG":
                    sell_res = await binance_live.place_market_sell(t.symbol, qty=t.qty)
                    if not sell_res.get("ok"):
                        await self._log("ERROR", f"live exit failed {t.symbol}: {sell_res.get('error')}")
                        continue
                    exit_order_id = str(sell_res.get("orderId"))
                    exit_price = sell_res.get("executed_price") or price

                # PnL math (LONG only for now).
                if t.direction == "LONG":
                    pnl_usd = (exit_price - t.entry_price) * t.qty
                else:
                    pnl_usd = (t.entry_price - exit_price) * t.qty
                pnl_pct = (pnl_usd / t.notional_usd) * 100 if t.notional_usd > 0 else 0.0
                won = pnl_usd > 0

                async with SessionLocal() as s:
                    live_t = await s.get(CandleTrade, t.id)
                    if not live_t:
                        continue
                    live_t.exit_price = exit_price
                    live_t.pnl_usd = round(pnl_usd, 6)
                    live_t.pnl_pct = round(pnl_pct, 4)
                    live_t.status = close_status
                    live_t.exit_order_id = exit_order_id
                    live_t.closed_at = datetime.now(timezone.utc)
                    live_t.current_price = exit_price

                    # Wallet update (paper only — live balance refreshes from API).
                    wallet = await self._get_wallet(s)
                    if not self.is_live:
                        # Credit back the notional + pnl.
                        wallet.paper_balance = round(
                            wallet.paper_balance + live_t.notional_usd + pnl_usd, 6
                        )
                        wallet.paper_total_pnl = round(wallet.paper_total_pnl + pnl_usd, 6)
                        wallet.paper_total_trades += 1
                        if won:
                            wallet.paper_wins += 1
                        else:
                            wallet.paper_losses += 1
                    else:
                        wallet.live_total_pnl = round(wallet.live_total_pnl + pnl_usd, 6)
                        wallet.live_total_trades += 1
                        if won:
                            wallet.live_wins += 1
                        else:
                            wallet.live_losses += 1
                    await s.commit()

                # Track losses for cooldown.
                if not won:
                    self._consecutive_losses += 1
                    self._daily_loss_total += abs(pnl_usd)
                    if self._consecutive_losses >= settings.CANDLE_MAX_CONSECUTIVE_LOSSES:
                        self._loss_cooldown_until = time.time() + settings.CANDLE_COOLDOWN_AFTER_LOSSES_MIN * 60
                        await self._log("WARNING",
                            f"loss cooldown engaged for {settings.CANDLE_COOLDOWN_AFTER_LOSSES_MIN}min "
                            f"({self._consecutive_losses} consecutive losses)"
                        )
                        self._consecutive_losses = 0
                    if self._daily_loss_total >= settings.CANDLE_MAX_DAILY_LOSS_USD:
                        self._loss_cooldown_until = time.time() + settings.CANDLE_DAILY_LOSS_COOLDOWN_MIN * 60
                        await self._log("WARNING",
                            f"daily loss cap ${self._daily_loss_total:.2f} hit — "
                            f"cooldown {settings.CANDLE_DAILY_LOSS_COOLDOWN_MIN}min"
                        )
                else:
                    self._consecutive_losses = 0

                await self._log("INFO",
                    f"CLOSED {close_status} {t.symbol} {t.direction} entry={t.entry_price:.4f} "
                    f"exit={exit_price:.4f} pnl=${pnl_usd:+.4f} ({pnl_pct:+.2f}%)"
                )
                await bus.publish("candle:trade:closed", {"id": t.id, "pnl_usd": pnl_usd})
            except Exception as e:
                await self._log("ERROR", f"manage position {t.symbol} failed: {e}")

    # ─────────────────── Wallet helpers ──────────────────────────

    async def _get_wallet(self, session) -> CandleWallet:
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
        return w

    async def _update_wallet_live_balance(self, usdt: float) -> None:
        async with SessionLocal() as s:
            w = await self._get_wallet(s)
            w.live_balance_usdt = round(usdt, 6)
            w.live_balance_updated_at = datetime.now(timezone.utc)
            await s.commit()


# Singletons — imported by routes.
candle_paper_bot = CandleBot(bot_kind="paper")
candle_live_bot = CandleBot(bot_kind="live")
