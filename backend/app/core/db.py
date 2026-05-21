from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from sqlalchemy import text
from app.core.config import normalize_db_url, settings


class Base(DeclarativeBase):
    pass


_db_url = normalize_db_url(settings.DATABASE_URL)
_sqlite_connect_args = {"timeout": 30} if _db_url.startswith("sqlite") else {}
engine = create_async_engine(_db_url, echo=False, connect_args=_sqlite_connect_args)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    from app.models import models  # noqa: F401  ensure mappers registered
    async with engine.begin() as conn:
        if _db_url.startswith("sqlite"):
            # Better concurrent-read/write behavior for SQLite under polling + bot writes.
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA busy_timeout=30000;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))
        # On full-disk SQLite, CREATE TABLE will fail. Swallow it so the app
        # still boots (the /maintenance/storage/cleanup endpoint can purge logs,
        # then a restart will create any pending tables successfully).
        try:
            await conn.run_sync(Base.metadata.create_all)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"init_db create_all skipped: {e}")
        try:
            await conn.execute(text("ALTER TABLE trades ADD COLUMN mode VARCHAR(20) DEFAULT 'paper'"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_trades ADD COLUMN mode VARCHAR(20) DEFAULT 'paper'"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN trade_balance FLOAT DEFAULT 20.0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN vault_balance FLOAT DEFAULT 0.0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN trade_cap_usd FLOAT DEFAULT 30.0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN vault_sweeps_count INTEGER DEFAULT 0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN last_sweep_at DATETIME"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN live_trade_balance FLOAT DEFAULT 0.0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN live_vault_balance FLOAT DEFAULT 0.0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN live_trade_cap_usd FLOAT DEFAULT 30.0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN live_vault_sweeps_count INTEGER DEFAULT 0"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN live_last_sweep_at DATETIME"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN live_last_withdraw_at DATETIME"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE poly_wallet ADD COLUMN live_withdrawn_total FLOAT DEFAULT 0.0"))
        except Exception:
            pass
        candle_trade_columns = [
            ("current_price", "FLOAT"),
            ("exit_price", "FLOAT"),
            ("pnl_usd", "FLOAT DEFAULT 0.0"),
            ("pnl_pct", "FLOAT DEFAULT 0.0"),
            ("status", "VARCHAR(20) DEFAULT 'OPEN'"),
            ("htf_bias", "VARCHAR(10) DEFAULT ''"),
            ("setup_type", "VARCHAR(30) DEFAULT ''"),
            ("confidence", "FLOAT DEFAULT 0.0"),
            ("rr_target", "FLOAT DEFAULT 2.0"),
            ("reasoning", "TEXT DEFAULT ''"),
            ("entry_order_id", "VARCHAR(64)"),
            ("exit_order_id", "VARCHAR(64)"),
            ("mode", "VARCHAR(20) DEFAULT 'paper'"),
            ("closed_at", "DATETIME"),
        ]
        for name, ddl in candle_trade_columns:
            try:
                await conn.execute(text(f"ALTER TABLE candle_trades ADD COLUMN {name} {ddl}"))
            except Exception:
                pass
        candle_wallet_columns = [
            ("paper_starting_balance", f"FLOAT DEFAULT {settings.CANDLE_PAPER_STARTING_BALANCE}"),
            ("live_balance_usdt", "FLOAT DEFAULT 0.0"),
            ("live_balance_updated_at", "DATETIME"),
            ("paper_total_pnl", "FLOAT DEFAULT 0.0"),
            ("paper_total_trades", "INTEGER DEFAULT 0"),
            ("paper_wins", "INTEGER DEFAULT 0"),
            ("paper_losses", "INTEGER DEFAULT 0"),
            ("live_total_pnl", "FLOAT DEFAULT 0.0"),
            ("live_total_trades", "INTEGER DEFAULT 0"),
            ("live_wins", "INTEGER DEFAULT 0"),
            ("live_losses", "INTEGER DEFAULT 0"),
        ]
        for name, ddl in candle_wallet_columns:
            try:
                await conn.execute(text(f"ALTER TABLE candle_wallet ADD COLUMN {name} {ddl}"))
            except Exception:
                pass
    try:
        async with SessionLocal() as session:
            from app.services.wallet import ensure_wallet_initialized
            from app.services.poly_wallet import ensure_wallet_initialized as ensure_poly_wallet_initialized
            await ensure_wallet_initialized(session)
            await ensure_poly_wallet_initialized(session)
            from app.models.models import CandleWallet
            from sqlalchemy import select
            candle_wallet = (
                await session.execute(select(CandleWallet).where(CandleWallet.id == 1))
            ).scalar_one_or_none()
            if candle_wallet is None:
                session.add(CandleWallet(
                    id=1,
                    paper_balance=settings.CANDLE_PAPER_STARTING_BALANCE,
                    paper_starting_balance=settings.CANDLE_PAPER_STARTING_BALANCE,
                ))
            await session.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"init_db wallet seed skipped: {e}")


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
