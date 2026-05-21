"""Binance public market data fetcher (no API key required).

Fetches OHLCV candle data and current prices from Binance's public REST API.
Uses httpx for async HTTP and caches recent klines in-memory to reduce calls.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Binance public REST endpoints. Override with BINANCE_HOST (e.g. relay URL).
_BINANCE_BASE = settings.BINANCE_HOST or "https://api.binance.com"

# In-memory cache: (symbol, interval) → (fetched_at_epoch, klines)
# Klines list contains [open_time_ms, open, high, low, close, volume, ...].
_CACHE: dict[tuple[str, str], tuple[float, list[list[Any]]]] = {}
_CACHE_TTL_SECONDS = 8  # 5m candles update every 5 minutes; 8s cache is fine.

_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_PRICE_CACHE_TTL_SECONDS = 2

_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=_BINANCE_BASE,
            timeout=httpx.Timeout(15.0, connect=5.0),
            http2=True,
            headers={"User-Agent": "amta-candle-bot/1.0"},
        )
    return _client


async def get_klines(
    symbol: str,
    interval: str = "5m",
    limit: int = 200,
    use_cache: bool = True,
) -> list[dict]:
    """Fetch recent candles for a symbol.

    Returns a list of dicts:
        {"t": open_time_ms, "o": open, "h": high, "l": low, "c": close, "v": volume}

    Binance kline format:
        [open_time, open, high, low, close, volume, close_time, ...]
    """
    symbol = symbol.upper().strip()
    interval = interval.strip()
    key = (symbol, interval)
    now = time.time()
    if use_cache and key in _CACHE:
        fetched_at, cached = _CACHE[key]
        if now - fetched_at < _CACHE_TTL_SECONDS and len(cached) >= limit:
            return _format_klines(cached[-limit:])

    async with _lock:
        # Re-check after acquiring lock to avoid duplicate fetches.
        if use_cache and key in _CACHE:
            fetched_at, cached = _CACHE[key]
            if now - fetched_at < _CACHE_TTL_SECONDS and len(cached) >= limit:
                return _format_klines(cached[-limit:])

        client = await _get_client()
        try:
            res = await client.get(
                "/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
            )
            res.raise_for_status()
            raw = res.json()
        except Exception as e:
            logger.error(f"Binance klines fetch failed for {symbol} {interval}: {e}")
            # On failure return last cached data if available, else empty.
            if key in _CACHE:
                return _format_klines(_CACHE[key][1][-limit:])
            return []

        _CACHE[key] = (now, raw)
        return _format_klines(raw[-limit:])


def _format_klines(raw: list[list[Any]]) -> list[dict]:
    """Convert Binance raw kline arrays to dict format."""
    out = []
    for k in raw:
        try:
            out.append({
                "t": int(k[0]),
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return out


async def get_price(symbol: str) -> float | None:
    """Get current best-bid/ask midpoint price for a symbol."""
    symbol = symbol.upper().strip()
    now = time.time()
    if symbol in _PRICE_CACHE:
        fetched_at, p = _PRICE_CACHE[symbol]
        if now - fetched_at < _PRICE_CACHE_TTL_SECONDS:
            return p
    client = await _get_client()
    try:
        res = await client.get("/api/v3/ticker/price", params={"symbol": symbol})
        res.raise_for_status()
        data = res.json()
        price = float(data.get("price", 0))
        if price > 0:
            _PRICE_CACHE[symbol] = (now, price)
            return price
    except Exception as e:
        logger.warning(f"Binance ticker fetch failed for {symbol}: {e}")
    return None


async def get_symbols_info() -> list[dict]:
    """Get tradeable symbols and their tick/lot rules."""
    client = await _get_client()
    try:
        res = await client.get("/api/v3/exchangeInfo")
        res.raise_for_status()
        data = res.json()
        return data.get("symbols", [])
    except Exception as e:
        logger.error(f"Binance exchangeInfo fetch failed: {e}")
        return []


async def close():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
