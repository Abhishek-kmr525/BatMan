"""Test bootstrap.

Forces the test process to use a *separate* SQLite file and an isolated
Chroma directory so the running dev server's data isn't touched.
"""
import os
import sys
import tempfile
from pathlib import Path

# Make sure backend/ is on sys.path so `import app...` works regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Override env BEFORE importing anything from app.*
_tmp = Path(tempfile.gettempdir()) / "amta-tests"
_tmp.mkdir(exist_ok=True)
os.environ["AMTA_TESTING"] = "1"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/test.db"
os.environ["CHROMA_DIR"] = str(_tmp / "chroma")
os.environ["KALSHI_DEMO"] = "true"
os.environ["KALSHI_PAPER_MODE"] = "true"
os.environ["ANTHROPIC_API_KEY"] = ""  # force heuristic path
os.environ["KALSHI_KEY_ID"] = ""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import pytest
from app.core.db import Base, SessionLocal, engine
from app.services import strategies


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    # Fresh schema per test
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        yield s


@pytest.fixture(autouse=True)
def _reset_strategies():
    strategies.deactivate_all()
    yield
    strategies.deactivate_all()
