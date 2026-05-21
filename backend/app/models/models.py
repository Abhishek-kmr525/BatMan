import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    market_title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), default="")
    direction: Mapped[str] = mapped_column(String(10))  # YES or NO
    amount: Mapped[float] = mapped_column(Float, default=1.00)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    agent_score: Mapped[int] = mapped_column(Integer, default=0)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), default="bot")  # bot | quick_trade
    mode: Mapped[str] = mapped_column(String(20), default="paper", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Wallet(Base):
    __tablename__ = "wallet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    balance: Mapped[float] = mapped_column(Float, default=10000.00)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.00)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class BotLog(Base):
    __tablename__ = "bot_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    source_file: Mapped[str] = mapped_column(Text, unique=True)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolyTrade(Base):
    __tablename__ = "poly_trades"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    market_title: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(10))  # YES or NO
    amount: Mapped[float] = mapped_column(Float, default=1.00)
    entry_price: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    agent_score: Mapped[int] = mapped_column(Integer, default=0)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(20), default="paper", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PolyWallet(Base):
    __tablename__ = "poly_wallet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    balance: Mapped[float] = mapped_column(Float, default=20.00)
    trade_balance: Mapped[float] = mapped_column(Float, default=20.00)
    vault_balance: Mapped[float] = mapped_column(Float, default=0.00)
    trade_cap_usd: Mapped[float] = mapped_column(Float, default=30.00)
    vault_sweeps_count: Mapped[int] = mapped_column(Integer, default=0)
    last_sweep_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.00)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    live_trade_balance: Mapped[float] = mapped_column(Float, default=0.00)
    live_vault_balance: Mapped[float] = mapped_column(Float, default=0.00)
    live_trade_cap_usd: Mapped[float] = mapped_column(Float, default=30.00)
    live_vault_sweeps_count: Mapped[int] = mapped_column(Integer, default=0)
    live_last_sweep_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    live_last_withdraw_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    live_withdrawn_total: Mapped[float] = mapped_column(Float, default=0.00)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class PolyWithdrawJob(Base):
    __tablename__ = "poly_withdraw_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(140), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[str] = mapped_column(String(16), default="manual")  # auto|manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PolyVaultEvent(Base):
    __tablename__ = "poly_vault_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    event_type: Mapped[str] = mapped_column(String(40), index=True)  # SWEEP|WITHDRAW_*|UNLOCK
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class CandleTrade(Base):
    """Crypto candle-strategy trade (Binance BTC/ETH spot)."""
    __tablename__ = "candle_trades"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    symbol: Mapped[str] = mapped_column(String(20), index=True)  # BTCUSDT, ETHUSDT
    interval: Mapped[str] = mapped_column(String(8))  # 5m, 15m, 1h
    direction: Mapped[str] = mapped_column(String(10))  # LONG or SHORT
    qty: Mapped[float] = mapped_column(Float, default=0.0)  # base asset quantity (BTC/ETH)
    notional_usd: Mapped[float] = mapped_column(Float, default=0.0)  # USD value at entry
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    # Strategy metadata
    htf_bias: Mapped[str] = mapped_column(String(10), default="")  # up, down, mixed
    setup_type: Mapped[str] = mapped_column(String(30), default="")  # liquidity_sweep_bos
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rr_target: Mapped[float] = mapped_column(Float, default=2.0)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    # Live order tracking
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="paper", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandleWallet(Base):
    """Simulated paper wallet + cached live Binance balance for candle bot."""
    __tablename__ = "candle_wallet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Paper-mode simulated balance
    paper_balance: Mapped[float] = mapped_column(Float, default=1000.00)
    paper_starting_balance: Mapped[float] = mapped_column(Float, default=1000.00)
    # Live-mode cached balance (refreshed from Binance API on each tick)
    live_balance_usdt: Mapped[float] = mapped_column(Float, default=0.00)
    live_balance_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Aggregate stats — paper
    paper_total_pnl: Mapped[float] = mapped_column(Float, default=0.00)
    paper_total_trades: Mapped[int] = mapped_column(Integer, default=0)
    paper_wins: Mapped[int] = mapped_column(Integer, default=0)
    paper_losses: Mapped[int] = mapped_column(Integer, default=0)
    # Aggregate stats — live
    live_total_pnl: Mapped[float] = mapped_column(Float, default=0.00)
    live_total_trades: Mapped[int] = mapped_column(Integer, default=0)
    live_wins: Mapped[int] = mapped_column(Integer, default=0)
    live_losses: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
