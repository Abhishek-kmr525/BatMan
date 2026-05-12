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
        await conn.run_sync(Base.metadata.create_all)
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
    async with SessionLocal() as session:
        from app.services.wallet import ensure_wallet_initialized
        from app.services.poly_wallet import ensure_wallet_initialized as ensure_poly_wallet_initialized
        await ensure_wallet_initialized(session)
        await ensure_poly_wallet_initialized(session)
        await session.commit()


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
