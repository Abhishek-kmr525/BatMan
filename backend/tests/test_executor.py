"""Executor tests — open / close, P&L math, position cap."""
import pytest

from app.agent.analyzer import Analysis
from app.services import executor, wallet
from app.services.kalshi import Market


class StubKalshi:
    """In-memory Kalshi for tests — controllable current price."""
    def __init__(self):
        self.current = {}  # ticker -> yes_price (0..1)
        self.orders = []

    async def place_order(self, **kw):
        self.orders.append(kw)
        return {"order_id": "stub", "status": "filled"}

    async def current_price(self, ticker, side):
        p = self.current.get(ticker, 0.50)
        return p if side.upper() == "YES" else round(1 - p, 4)

    async def get_market(self, ticker):
        p = self.current.get(ticker, 0.50)
        return Market(ticker, ticker, "X", p, round(1-p, 4), 100, 0, 3600, {})


def _market(ticker="T", yes=0.20):
    return Market(ticker, ticker, "X", yes, round(1-yes,4), 100, 0, 3600, {})


def _analysis(action="BUY_YES", score=80, entry=0.20, target=0.30, stop=0.14):
    return Analysis(
        score=score, action=action, confidence=0.8,
        entry_price=entry, target_exit_price=target, stop_loss_price=stop,
        reasoning="test", knowledge_sources=[], raw={},
    )


# ----------------- open -----------------
async def test_open_trade_debits_wallet(session):
    k = StubKalshi()
    m = _market(yes=0.12)
    t = await executor.open_trade(session, k, m, _analysis(entry=0.12))
    await session.commit()
    assert t is not None
    assert t.direction == "YES"
    assert t.entry_price == 0.12
    w = await wallet.get_wallet(session)
    assert w.balance == pytest.approx(9999.00)


async def test_open_trade_skips_below_threshold(session):
    k = StubKalshi()
    t = await executor.open_trade(
        session, k, _market(), _analysis(score=50)
    )
    assert t is None


async def test_open_trade_respects_min_score_override(session):
    k = StubKalshi()
    # score 50 normally rejected; with min_score=0 it goes through
    t = await executor.open_trade(
        session, k, _market(), _analysis(score=50), min_score=0
    )
    assert t is not None


async def test_open_trade_blocks_duplicate_market(session):
    k = StubKalshi()
    t1 = await executor.open_trade(session, k, _market("DUP"), _analysis())
    await session.commit()
    t2 = await executor.open_trade(session, k, _market("DUP"), _analysis())
    assert t1 is not None
    assert t2 is None


async def test_open_trade_blocks_duplicate_event_title(session):
    k = StubKalshi()
    m1 = Market("GAME-LAL", "Game 6: Los Angeles L at Houston Winner?", "NBA", 0.84, 0.16, 100, 0, 3600, {})
    m2 = Market("GAME-HOU", "Game 6: Los Angeles L at Houston Winner?", "NBA", 0.15, 0.85, 100, 0, 3600, {})
    t1 = await executor.open_trade(session, k, m1, _analysis(action="BUY_NO", entry=0.16))
    await session.commit()
    trade, reason = await executor.open_trade_with_reason(session, k, m2, _analysis(entry=0.15))
    assert t1 is not None
    assert trade is None
    assert reason == executor.OpenTradeRejectReason.DUPLICATE_EVENT_POSITION


async def test_open_trade_skips_unless_buy_action(session):
    k = StubKalshi()
    assert await executor.open_trade(session, k, _market(), _analysis(action="SKIP")) is None


# --------------- close / PnL math ---------------
async def test_close_take_profit_books_positive_pnl(session):
    k = StubKalshi()
    m = _market("WIN", yes=0.10)
    t = await executor.open_trade(session, k, m, _analysis(entry=0.10, target=0.20))
    await session.commit()

    # price reaches target → take-profit
    k.current["WIN"] = 0.20
    closed = await executor.evaluate_exit(session, k, t)
    await session.commit()
    assert closed is not None
    assert closed.status == "CLOSED_TAKE_PROFIT"
    # $1 / $0.10 = 10 contracts × $0.20 = $2.00 payout, profit = $1.00
    assert closed.pnl == pytest.approx(1.00, abs=0.01)
    w = await wallet.get_wallet(session)
    # Started 10000, debited 1, credited 2, recorded +1 pnl, +1 win
    assert w.balance == pytest.approx(10001.00, abs=0.01)
    assert w.wins == 1


async def test_close_stop_loss_books_negative_pnl(session):
    k = StubKalshi()
    m = _market("LOSS", yes=0.20)
    t = await executor.open_trade(session, k, m, _analysis(entry=0.20, target=0.40, stop=0.10))
    await session.commit()

    k.current["LOSS"] = 0.10  # hit stop
    closed = await executor.evaluate_exit(session, k, t)
    await session.commit()
    assert closed.status == "CLOSED_STOP_LOSS"
    # 5 contracts × 0.10 = 0.50 payout, loss = -0.50
    assert closed.pnl == pytest.approx(-0.50, abs=0.01)
    w = await wallet.get_wallet(session)
    assert w.losses == 1


async def test_close_at_entry_books_zero_pnl(session):
    """Reproduces 'why is P&L zero' scenario — exit at same price as entry."""
    k = StubKalshi()
    m = _market("FLAT", yes=0.30)
    t = await executor.open_trade(session, k, m, _analysis(entry=0.30, target=0.30, stop=0.10))
    await session.commit()

    # immediate take-profit (target == entry)
    k.current["FLAT"] = 0.30
    closed = await executor.evaluate_exit(session, k, t)
    await session.commit()
    # $1 / 0.30 = 3.33 contracts × 0.30 = $1.00 payout → 0 pnl
    assert closed.pnl == pytest.approx(0.0, abs=0.001)


# --------------- cap ---------------
async def test_position_cap_enforced(session, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MAX_CONCURRENT_POSITIONS", 2)
    k = StubKalshi()
    a = _analysis()
    t1 = await executor.open_trade(session, k, _market("A"), a)
    t2 = await executor.open_trade(session, k, _market("B"), a)
    t3 = await executor.open_trade(session, k, _market("C"), a)
    assert t1 is not None and t2 is not None
    assert t3 is None  # cap hit


async def test_open_trade_with_reason_duplicate_market(session):
    k = StubKalshi()
    t1, r1 = await executor.open_trade_with_reason(session, k, _market("DUP2"), _analysis())
    await session.commit()
    t2, r2 = await executor.open_trade_with_reason(session, k, _market("DUP2"), _analysis())
    assert t1 is not None and r1 is None
    assert t2 is None
    assert r2 == executor.OpenTradeRejectReason.DUPLICATE_MARKET_POSITION


async def test_open_trade_with_reason_position_cap(session, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MAX_CONCURRENT_POSITIONS", 1)
    k = StubKalshi()
    t1, r1 = await executor.open_trade_with_reason(session, k, _market("CAPA"), _analysis())
    t2, r2 = await executor.open_trade_with_reason(session, k, _market("CAPB"), _analysis())
    assert t1 is not None and r1 is None
    assert t2 is None
    assert r2 == executor.OpenTradeRejectReason.POSITION_CAP_REACHED
