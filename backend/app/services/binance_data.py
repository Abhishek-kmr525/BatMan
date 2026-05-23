"""Binance public market data fetcher (no API key required).

Resilient behavior:
- Supports optional BINANCE_HOST override.
- Falls back across official Binance public hosts on network/DNS failures.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

# In-memory cache: (symbol, interval) -> (fetched_at_epoch, klines raw arrays)
_CACHE: dict[tuple[str, str], tuple[float, list[list[Any]]]] = {}
_CACHE_TTL_SECONDS = 8

_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_PRICE_CACHE_TTL_SECONDS = 2

_clients: dict[str, httpx.AsyncClient] = {}
_lock = asyncio.Lock()


def _candidate_bases() -> list[str]:
    bases: list[str] = []
    host = (settings.BINANCE_HOST or "").strip().rstrip("/")
    if host:
        bases.append(host)
    for b in _DEFAULT_BASES:
        if b not in bases:
            bases.append(b)
    return bases


async def _get_client(base_url: str) -> httpx.AsyncClient:
    c = _clients.get(base_url)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            http2=True,
            headers={"User-Agent": "amta-candle-bot/1.0"},
        )
        _clients[base_url] = c
    return c


def _format_klines(raw: list[list[Any]]) -> list[dict]:
    out = []
    for k in raw:
        try:
            out.append(
                {
                    "t": int(k[0]),
                    "o": float(k[1]),
                    "h": float(k[2]),
                    "l": float(k[3]),
                    "c": float(k[4]),
                    "v": float(k[5]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return out


async def get_klines(
    symbol: str,
    interval: str = "5m",
    limit: int = 200,
    use_cache: bool = True,
) -> list[dict]:
    symbol = symbol.upper().strip()
    interval = interval.strip()
    key = (symbol, interval)
    now = time.time()

    if use_cache and key in _CACHE:
        fetched_at, cached = _CACHE[key]
        if now - fetched_at < _CACHE_TTL_SECONDS and len(cached) >= limit:
            return _format_klines(cached[-limit:])

    async with _lock:
        if use_cache and key in _CACHE:
            fetched_at, cached = _CACHE[key]
            if now - fetched_at < _CACHE_TTL_SECONDS and len(cached) >= limit:
                return _format_klines(cached[-limit:])

        raw: list[list[Any]] | None = None
        last_error: Exception | None = None
        for base in _candidate_bases():
            client = await _get_client(base)
            try:
                res = await client.get(
                    "/api/v3/klines",
                    params={"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
                )
                res.raise_for_status()
                raw = res.json()
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    "Binance klines fetch failed via %s for %s %s: %s",
                    base,
                    symbol,
                    interval,
                    e,
                )

        if raw is None:
            logger.error("Binance klines fetch failed for %s %s: %s", symbol, interval, last_error)
            if key in _CACHE:
                return _format_klines(_CACHE[key][1][-limit:])
            return []

        _CACHE[key] = (now, raw)
        return _format_klines(raw[-limit:])


async def get_price(symbol: str) -> float | None:
    symbol = symbol.upper().strip()
    now = time.time()
    if symbol in _PRICE_CACHE:
        fetched_at, p = _PRICE_CACHE[symbol]
        if now - fetched_at < _PRICE_CACHE_TTL_SECONDS:
            return p

    for base in _candidate_bases():
        client = await _get_client(base)
        try:
            res = await client.get("/api/v3/ticker/price", params={"symbol": symbol})
            res.raise_for_status()
            data = res.json()
            price = float(data.get("price", 0))
            if price > 0:
                _PRICE_CACHE[symbol] = (now, price)
                return price
        except Exception as e:
            logger.warning("Binance ticker fetch failed via %s for %s: %s", base, symbol, e)
    return None


async def get_symbols_info() -> list[dict]:
    for base in _candidate_bases():
        client = await _get_client(base)
        try:
            res = await client.get("/api/v3/exchangeInfo")
            res.raise_for_status()
            data = res.json()
            return data.get("symbols", [])
        except Exception as e:
            logger.warning("Binance exchangeInfo fetch failed via %s: %s", base, e)
    return []


async def close():
    for base, c in list(_clients.items()):
        if c and not c.is_closed:
            await c.aclose()
        _clients.pop(base, None)
