import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.models import CandleTrade
from app.services.bot_candle import CandleBot
from app.services.canary_guard import check_candle_canary
from app.services.candle_strategy import CandleSignal


def _long_signal(entry: float = 100.0, stop: float = 99.0) -> CandleSignal:
    risk = entry - stop
    return CandleSignal(
        direction="LONG",
        confidence=0.8,
        entry_price=entry,
        stop_loss=stop,
        take_profit=entry + risk * 2,
        rr_ratio=2.0,
        htf_bias="up",
        setup_type="test",
        reasoning="test signal",
    )


async def test_candle_paper_open_debits_wallet_and_records_trade(session, monkeypatch):
    monkeypatch.setattr(settings, "CANDLE_PAPER_STARTING_BALANCE", 1000.0)
    monkeypatch.setattr(settings, "CANDLE_RISK_PER_TRADE_PCT", 0.01)
    monkeypatch.setattr(settings, "CANDLE_PAPER_MAX_NOTIONAL_USD", 5.0)
    monkeypatch.setattr(settings, "CANDLE_PAPER_MAX_NOTIONAL_PCT_EQUITY", 0.25)

    bot = CandleBot(bot_kind="paper")
    opened = await bot._open_position("BTCUSDT", _long_signal(), live_balance=None)

    assert opened is True
    wallet = await bot._get_wallet(session)
    assert wallet.paper_balance == pytest.approx(995.0)

    rows = (await session.execute(
        select(CandleTrade).where(CandleTrade.symbol == "BTCUSDT")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].notional_usd == pytest.approx(5.0)


async def test_candle_canary_blocks_order_above_limit(session, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_CANARY_ENABLED", True)
    monkeypatch.setattr(settings, "CANDLE_CANARY_MAX_ORDER_USD", 5.0)

    allowed, reason, meta = await check_candle_canary(
        session,
        mode="live_armed",
        order_usd=6.0,
    )

    assert allowed is False
    assert reason == "canary_order_size_limit"
    assert meta["order_usd"] == 6.0


async def test_candle_canary_blocks_total_live_exposure(session, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_CANARY_ENABLED", True)
    monkeypatch.setattr(settings, "CANDLE_CANARY_MAX_ORDER_USD", 100.0)
    monkeypatch.setattr(settings, "CANDLE_CANARY_MAX_TOTAL_EXPOSURE_USD", 10.0)

    session.add(CandleTrade(
        symbol="BTCUSDT",
        interval="5m",
        direction="LONG",
        qty=0.05,
        notional_usd=8.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        mode="live",
        status="OPEN",
    ))
    await session.commit()

    allowed, reason, meta = await check_candle_canary(
        session,
        mode="live_armed",
        order_usd=3.0,
    )

    assert allowed is False
    assert reason == "canary_total_exposure_limit"
    assert meta["open_exposure_usd"] == pytest.approx(8.0)
