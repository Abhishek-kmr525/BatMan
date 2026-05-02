"""Wallet service tests."""
import pytest

from app.services import wallet


async def test_initial_balance_is_seeded(session):
    w = await wallet.get_wallet(session)
    await session.commit()
    assert w.balance == pytest.approx(10000.00)
    assert w.total_pnl == 0.0
    assert w.total_trades == 0


async def test_debit_then_credit(session):
    await wallet.debit(session, 1.00)
    await wallet.credit(session, 0.65)
    await session.commit()
    w = await wallet.get_wallet(session)
    assert w.balance == pytest.approx(9999.65)


async def test_debit_insufficient_raises(session):
    with pytest.raises(ValueError):
        await wallet.debit(session, 10_000_000)


async def test_record_close_updates_stats(session):
    await wallet.record_close(session, pnl=0.25, win=True)
    await wallet.record_close(session, pnl=-0.30, win=False)
    await session.commit()
    w = await wallet.get_wallet(session)
    assert w.total_trades == 2
    assert w.wins == 1
    assert w.losses == 1
    assert w.total_pnl == pytest.approx(-0.05)


async def test_deposit_positive_only(session):
    with pytest.raises(ValueError):
        await wallet.deposit(session, 0)
    with pytest.raises(ValueError):
        await wallet.deposit(session, -50)
    w = await wallet.deposit(session, 250)
    await session.commit()
    assert w.balance == pytest.approx(10250.00)
