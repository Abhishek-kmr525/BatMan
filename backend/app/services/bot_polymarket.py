"""Phase-1 paper bot for Polymarket (separate from Kalshi bot)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.models import BotLog, PolyTrade
from app.services.events import bus
from app.services.mode_guard import mode_guard
from app.services.canary_guard import check_polymarket_canary
from app.services.polymarket import get_polymarket
from app.services import poly_wallet
from app.services.poly_analyzer import analyze_polymarket
from app.services.poly_live import place_live_order
from app.services.poly_live_vault import sweep_live_excess_if_needed
from app.services.poly_live_vault import compute_auto_withdraw_eligibility, execute_withdraw_job, request_withdraw_job
from app.services.risk_engine import check_polymarket_entry_risk

State = Literal["IDLE", "SCANNING", "ANALYZING", "EXECUTING", "STOPPED"]


class PolymarketBot:
    # Cooldown: market_id → unix timestamp of last live order placed.
    # Prevents double-ordering the same market across ticks (e.g. if DB trade
    # is transiently closed by _manage_positions before the next scan).
    _LIVE_ORDER_COOLDOWN_SECS = 1200  # 20 minutes

    def __init__(self, *, bot_kind: Literal["paper", "live"] = "paper") -> None:
        self.bot_kind: Literal["paper", "live"] = bot_kind
        self.trade_mode: str = "live" if bot_kind == "live" else "paper"
        self.is_live_bot: bool = bot_kind == "live"
        self.state: State = "STOPPED"
        self.started_at: datetime | None = None
        self.trades_today = 0
        self.scanned_markets_today = 0
        self.last_scan_count = 0
        self.last_candidate_count = 0
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Per-session live-order cooldown (market_id → epoch seconds).
        self._recently_ordered: dict[str, float] = {}

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
            "bot_kind": self.bot_kind,
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
        payload = {"platform": "polymarket", "bot_kind": self.bot_kind, **(meta or {})}
        # Logging must never crash the trading loop.
        for attempt in range(3):
            try:
                async with SessionLocal() as s:
                    s.add(BotLog(level=level, message=message, metadata_json=payload))
                    await s.commit()
                break
            except OperationalError as e:
                err = str(e).lower()
                if "database is locked" in err and attempt < 2:
                    await asyncio.sleep(0.12 * (attempt + 1))
                    continue
                break
            except Exception:
                break
        await bus.publish(
            "agent:log",
            {"level": level, "message": message, "metadata": payload, "ts": datetime.now(timezone.utc).isoformat()},
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
        if self.is_live_bot and not settings.POLYMARKET_LIVE_ENABLED:
            await self._log("INFO", "polymarket live bot idle: POLYMARKET_LIVE_ENABLED=false")
            return
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
        await self._log("INFO", f"polymarket tick: scanned={len(markets)} candidates={len(candidates)}")
        async with SessionLocal() as s:
            await self._manage_positions(s, poly)
            open_rows = (
                await s.execute(
                    select(PolyTrade).where(
                        PolyTrade.status == "OPEN",
                        PolyTrade.mode == self.trade_mode,
                    )
                )
            ).scalars().all()
            if len(open_rows) >= settings.POLYMARKET_MAX_OPEN_POSITIONS:
                return

            open_market_ids = {t.market_id for t in open_rows}
            wallet = await poly_wallet.get_wallet(s)
            # Vault-first guard (paper only): if a close/deposit pushed funds over
            # cap, sweep immediately and skip any new opens this tick.
            if not self.is_live_bot:
                wallet, swept_now = await poly_wallet.enforce_vault_cap(s)
                if swept_now > 0:
                    await s.commit()
                    await self._log(
                        "INFO",
                        f"VAULT_SWEEP paper +${swept_now:.2f} (trade->{wallet.trade_cap_usd:.2f}, "
                        f"vault->{wallet.vault_balance:.2f})",
                    )
                    await self._log(
                        "INFO",
                        "polymarket vault guard: threshold reached; manage-only tick (no new opens)",
                    )
                    await bus.publish("polymarket:wallet:updated", {"balance": wallet.balance})
                    return

            # Fetch live CLOB balance once per tick to guard against placing
            # orders that will fail due to insufficient on-chain funds.
            _current_mode = "live_armed" if self.is_live_bot else "paper"
            _live_clob_balance: float | None = None
            if _current_mode == "live_armed":
                from app.services.poly_live import get_live_balance
                _live_clob_balance = await get_live_balance()
                if _live_clob_balance is not None:
                    await self._log(
                        "INFO",
                        f"polymarket live CLOB balance: ${_live_clob_balance:.4f}",
                    )
                    # Live vault sweep/partition on every fresh balance read.
                    if settings.POLY_LIVE_VAULT_ENABLED:
                        wallet, _ = await sweep_live_excess_if_needed(s, wallet, _live_clob_balance)
                        await s.commit()
                        # Auto-withdraw pipeline (single-flight in service).
                        eligible, _reason, amount = await compute_auto_withdraw_eligibility(
                            s, wallet, open_positions=len(open_rows)
                        )
                        if eligible and amount > 0:
                            wjob = await request_withdraw_job(s, amount_usd=amount, requested_by="auto")
                            await s.flush()
                            await execute_withdraw_job(s, wjob, wallet)
                            await s.commit()

            opened_this_tick = 0
            # In live mode cap new opens to 4 per tick to avoid flooding.
            _is_live_tick = _current_mode == "live_armed"
            max_per_tick = 4 if _is_live_tick else max(1, getattr(settings, "POLYMARKET_MAX_OPENS_PER_TICK", 5))

            # Sync paper wallet balance to real CLOB balance so AMTA shows
            # the correct figure instead of a stale paper-simulation number.
            if _is_live_tick and _live_clob_balance is not None and _live_clob_balance > 0:
                wallet.balance = round(_live_clob_balance, 4)

            for m in candidates[:25]:
                title_short = " ".join((m.title or "").split())
                title_short = f"{title_short[:88]}…" if len(title_short) > 88 else title_short
                await self._log(
                    "INFO",
                    f"scan {m.id} | {title_short} | YES {m.yes_price:.3f} / NO {m.no_price:.3f}",
                )
                if m.id in open_market_ids:
                    await self._log("INFO", f"skip {m.id}: already open")
                    continue
                # Per-session cooldown: skip market ordered within last 20 min.
                _last_ordered = self._recently_ordered.get(m.id)
                if _last_ordered is not None and (time.time() - _last_ordered) < self._LIVE_ORDER_COOLDOWN_SECS:
                    await self._log(
                        "INFO",
                        f"polymarket cooldown skip {m.id}: ordered {int(time.time()-_last_ordered)}s ago",
                    )
                    continue
                if len(open_rows) + opened_this_tick >= settings.POLYMARKET_MAX_OPEN_POSITIONS:
                    break

                # AI + candle + cheatsheet decision.
                try:
                    analysis = await asyncio.wait_for(analyze_polymarket(m), timeout=12)
                except asyncio.TimeoutError:
                    await self._log("ERROR", f"polymarket analyze timeout {m.id}")
                    continue
                action = (analysis.action or "SKIP").upper()
                await self._log("INFO", f"ai {m.id}: action={action} score={analysis.score}")
                if action not in {"BUY_YES", "BUY_NO"}:
                    await self._log("INFO", f"skip {m.id}: AI action {action}")
                    continue
                if analysis.score < settings.POLYMARKET_MIN_SCORE:
                    await self._log(
                        "INFO",
                        f"skip {m.id}: score {analysis.score} < min {settings.POLYMARKET_MIN_SCORE}",
                    )
                    continue

                side = "YES" if action == "BUY_YES" else "NO"
                entry = m.yes_price if side == "YES" else m.no_price

                if not self.is_live_bot:
                    await self._log("INFO", f"risk check {m.id}: begin")
                    try:
                        risk = await asyncio.wait_for(
                            check_polymarket_entry_risk(
                                s, m, score=analysis.score, open_positions=len(open_rows) + opened_this_tick
                            ),
                            timeout=5,
                        )
                    except asyncio.TimeoutError:
                        await self._log("ERROR", f"polymarket risk timeout {m.id}")
                        continue
                    await self._log("INFO", f"risk check {m.id}: {risk.reason}")
                    if not risk.allow:
                        await self._log("INFO", f"polymarket risk blocked {m.id}: {risk.reason}", risk=risk.meta)
                        continue

                self.state = "EXECUTING"
                # Independent paper/live bots share only kill-switch safety.
                if mode_guard.get("polymarket").kill_switch:
                    await self._log("INFO", "polymarket blocked open: kill switch ON")
                    continue
                mode = _current_mode
                is_live_mode = self.is_live_bot
                amount = settings.POLYMARKET_TRADE_AMOUNT_USD
                # Use the same Mode-A/Mode-B strategy path in both live and paper.
                # Live uses on-chain CLOB balance when known; paper uses wallet balance.
                mode_balance = (
                    wallet.live_trade_balance
                    if (is_live_mode and settings.POLY_LIVE_VAULT_ENABLED)
                    else (
                        _live_clob_balance
                        if (is_live_mode and _live_clob_balance is not None and _live_clob_balance > 0)
                        else wallet.balance
                    )
                )
                mode_b = (mode_balance >= 5.0) and (not settings.POLYMARKET_FORCE_MODE_A)
                CLOB_MIN_SIZE = 5.0

                if mode_b:
                    # MODE-B: use AI-recommended favourite with $5 size.
                    if entry < settings.POLYMARKET_MIN_FAVORITE_PRICE:
                        await self._log(
                            "INFO",
                            f"polymarket skip {m.id}: AI side {side}@{entry:.3f} "
                            f"< min_favorite {settings.POLYMARKET_MIN_FAVORITE_PRICE:.2f}",
                        )
                        continue
                    plan_amount = 5.00
                    plan_side = side
                    plan_entry = entry
                    raw_size = plan_amount / max(plan_entry, 0.001)
                    plan_size = max(raw_size, CLOB_MIN_SIZE)
                    effective_amount = round(plan_size * plan_entry, 4)
                    await self._log(
                        "INFO",
                        f"polymarket MODE-B (≥$5): ${plan_amount:.2f} on "
                        f"{plan_side}@{plan_entry:.3f} → {plan_size:.2f} contracts",
                    )
                else:
                    # MODE-A: force cheap underdog side with $1 sizing.
                    yes_p = getattr(m, "yes_price", None) or 0.5
                    no_p = getattr(m, "no_price", None) or 0.5
                    if yes_p <= no_p:
                        plan_side = "YES"
                        plan_entry = yes_p
                    else:
                        plan_side = "NO"
                        plan_entry = no_p

                    mode_a_max = float(getattr(settings, "POLYMARKET_MODE_A_MAX_UNDERDOG_PRICE", 0.20))
                    if plan_entry > mode_a_max:
                        await self._log(
                            "INFO",
                            f"polymarket MODE-A skip {m.id}: cheap side "
                            f"{plan_side}@{plan_entry:.3f} > ${mode_a_max:.2f} — "
                            f"need underdog ≤ ${mode_a_max:.2f}",
                        )
                        continue

                    raw_size = 1.00 / max(plan_entry, 0.001)
                    if raw_size < CLOB_MIN_SIZE:
                        await self._log(
                            "INFO",
                            f"polymarket MODE-A skip {m.id}: $1 → {raw_size:.2f} contracts "
                            f"< CLOB min 5 at {plan_entry:.3f}",
                        )
                        continue

                    plan_size = round(raw_size, 2)
                    effective_amount = round(plan_size * plan_entry, 4)
                    await self._log(
                        "INFO",
                        f"polymarket MODE-A (<$5): $1.00 on "
                        f"{plan_side}@{plan_entry:.3f} (underdog, {plan_size:.2f} contracts)",
                    )

                side = plan_side
                entry = plan_entry
                amount = effective_amount

                if is_live_mode:
                    if _live_clob_balance is None:
                        await self._log("INFO", f"polymarket live skip {m.id}: live balance unavailable")
                        continue
                    spendable_balance = float(_live_clob_balance)
                else:
                    spendable_balance = float(wallet.balance)
                if spendable_balance < effective_amount:
                    await self._log(
                        "INFO",
                        f"polymarket skip {m.id}: wallet ${spendable_balance:.2f} < ${effective_amount:.2f}",
                    )
                    continue

                canary_ok, canary_reason, canary_meta = await check_polymarket_canary(
                    s, mode=mode, order_usd=amount
                )
                if not canary_ok:
                    await self._log("INFO", f"polymarket canary blocked open: {canary_reason}", canary=canary_meta)
                    continue

                if is_live_mode:
                    raw_tokens = m.raw.get("clobTokenIds")
                    if isinstance(raw_tokens, str):
                        import json
                        try:
                            raw_tokens = json.loads(raw_tokens)
                        except Exception:
                            raw_tokens = []

                    if not raw_tokens or len(raw_tokens) < 2:
                        await self._log("ERROR", f"polymarket live order blocked: missing clobTokenIds for {m.id}")
                        continue

                    balance_known = _live_clob_balance is not None and _live_clob_balance > 0.10
                    token_id = raw_tokens[0] if side == "YES" else raw_tokens[1]

                    # Guard: CLOB on-chain balance must cover the order.
                    # No cushion — the returned free balance is already net of
                    # locked funds, so a straight comparison is correct.
                    if balance_known and effective_amount > _live_clob_balance:
                        await self._log(
                            "INFO",
                            f"polymarket live skip {m.id}: CLOB balance "
                            f"${_live_clob_balance:.2f} < ${effective_amount:.2f} needed",
                        )
                        continue

                    try:
                        live_res = await asyncio.wait_for(
                            place_live_order(
                                token_id=str(token_id),
                                price=entry,
                                size=plan_size,
                                side="BUY",
                            ),
                            timeout=15,
                        )
                    except asyncio.TimeoutError:
                        await self._log("ERROR", f"polymarket live order timeout {m.id}")
                        continue
                    if not live_res.get("ok"):
                        await self._log("ERROR", f"polymarket live order failed: {live_res.get('error')}")
                        continue

                    await self._log("INFO", f"polymarket live order placed {m.id}: {live_res.get('orderID')}")
                    # Record cooldown so we never re-order this market this session.
                    self._recently_ordered[m.id] = time.time()

                if not is_live_mode:
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
                    mode=self.trade_mode,
                )
                s.add(t)
                await s.commit()
                # Prevent re-processing the same market later in this tick.
                open_market_ids.add(m.id)
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
                if not is_live_mode:
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
        open_rows = (
            await s.execute(
                select(PolyTrade).where(
                    PolyTrade.status == "OPEN",
                    PolyTrade.mode == self.trade_mode,
                )
            )
        ).scalars().all()
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

                            # ── Stop Loss: position value dropped -30% ──────────
                            if cur <= t.entry_price * 0.70:
                                contracts = t.amount / max(t.entry_price, 0.01)
                                payout = contracts * cur
                                pnl = round(payout - t.amount, 4)
                                t.status = "CLOSED_STOP_LOSS"
                                t.exit_price = cur
                                t.pnl = pnl
                                t.closed_at = datetime.now(timezone.utc)

                                if self.is_live_bot:
                                    raw_tokens = data.get("clobTokenIds")
                                    if isinstance(raw_tokens, str):
                                        import json
                                        try:
                                            raw_tokens = json.loads(raw_tokens)
                                        except Exception:
                                            raw_tokens = []
                                    if raw_tokens and len(raw_tokens) >= 2:
                                        token_id = raw_tokens[0] if t.direction == "YES" else raw_tokens[1]
                                        size = round(t.amount / max(t.entry_price, 0.01), 2)
                                        live_res = await place_live_order(
                                            token_id=str(token_id), price=cur, size=size, side="SELL"
                                        )
                                        if not live_res.get("ok"):
                                            await self._log("ERROR", f"polymarket live sl exit failed: {live_res.get('error')}")
                                            # Do not mark closed without a confirmed live exit.
                                            continue

                                if not self.is_live_bot:
                                    _, swept = await poly_wallet.credit(s, payout)
                                    await poly_wallet.record_close(s, pnl, False)
                                    if swept > 0:
                                        w_after = await poly_wallet.get_wallet(s)
                                        await self._log(
                                            "INFO",
                                            f"VAULT_SWEEP paper +${swept:.2f} (trade->{w_after.trade_cap_usd:.2f}, vault->{w_after.vault_balance:.2f})",
                                        )
                                await s.commit()
                                await self._log(
                                    "INFO",
                                    f"polymarket SL-30% closed {t.id} entry={t.entry_price:.3f} "
                                    f"exit={cur:.3f} pnl={pnl:.3f}",
                                )
                                await bus.publish("polymarket:trade:closed", {"id": t.id, "pnl": pnl})
                                if not self.is_live_bot:
                                    w = await poly_wallet.get_wallet(s)
                                    await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
                                continue

                            # ── Take Profit: position value gained +15% ─────────
                            if cur >= t.entry_price * 1.15:
                                contracts = t.amount / max(t.entry_price, 0.01)
                                payout = contracts * cur
                                pnl = round(payout - t.amount, 4)
                                t.status = "CLOSED_TAKE_PROFIT"
                                t.exit_price = cur
                                t.pnl = pnl
                                t.closed_at = datetime.now(timezone.utc)
                                
                                if self.is_live_bot:
                                    raw_tokens = data.get("clobTokenIds")
                                    if isinstance(raw_tokens, str):
                                        import json
                                        try:
                                            raw_tokens = json.loads(raw_tokens)
                                        except Exception:
                                            raw_tokens = []
                                    if raw_tokens and len(raw_tokens) >= 2:
                                        token_id = raw_tokens[0] if t.direction == "YES" else raw_tokens[1]
                                        size = round(t.amount / max(t.entry_price, 0.01), 2)
                                        live_res = await place_live_order(token_id=str(token_id), price=cur, size=size, side="SELL")
                                        if not live_res.get("ok"):
                                            await self._log("ERROR", f"polymarket live tp exit failed: {live_res.get('error')}")
                                            # Do not mark closed without a confirmed live exit.
                                            continue

                                if not self.is_live_bot:
                                    _, swept = await poly_wallet.credit(s, payout)
                                    await poly_wallet.record_close(s, pnl, pnl > 0)
                                    if swept > 0:
                                        w_after = await poly_wallet.get_wallet(s)
                                        await self._log(
                                            "INFO",
                                            f"VAULT_SWEEP paper +${swept:.2f} (trade->{w_after.trade_cap_usd:.2f}, vault->{w_after.vault_balance:.2f})",
                                        )
                                await s.commit()
                                await self._log(
                                    "INFO",
                                    f"polymarket TP+15% closed {t.id} entry={t.entry_price:.3f} exit={cur:.3f} pnl={pnl:.3f}",
                                )
                                await bus.publish("polymarket:trade:closed", {"id": t.id, "pnl": pnl})
                                if not self.is_live_bot:
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
                            
                            if not self.is_live_bot:
                                _, swept = await poly_wallet.credit(s, t.amount + t.pnl)
                                await poly_wallet.record_close(s, t.pnl, win)
                                if swept > 0:
                                    w_after = await poly_wallet.get_wallet(s)
                                    await self._log(
                                        "INFO",
                                        f"VAULT_SWEEP paper +${swept:.2f} (trade->{w_after.trade_cap_usd:.2f}, vault->{w_after.vault_balance:.2f})",
                                    )
                            await s.commit()
                            
                            await self._log("INFO", f"polymarket closed {t.id} {status} pnl={t.pnl:.2f}")
                            await bus.publish("polymarket:trade:closed", {"id": t.id, "pnl": t.pnl})
                            if not self.is_live_bot:
                                w = await poly_wallet.get_wallet(s)
                                await bus.publish("polymarket:wallet:updated", {"balance": w.balance})
            except Exception as e:
                await self._log("ERROR", f"polymarket manage position {t.id} failed: {e}")



poly_paper_bot = PolymarketBot(bot_kind="paper")
poly_live_bot = PolymarketBot(bot_kind="live")
# Backward-compat alias (paper).
poly_bot = poly_paper_bot
