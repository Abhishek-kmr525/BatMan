from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import normalize_db_url, settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(normalize_db_url(settings.DATABASE_URL), echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    from app.models import models  # noqa: F401  ensure mappers registered
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        from app.services.wallet import ensure_wallet_initialized
        from app.services.poly_wallet import ensure_wallet_initialized as ensure_poly_wallet_initialized
        await ensure_wallet_initialized(session)
        await ensure_poly_wallet_initialized(session)
        await session.commit()


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
