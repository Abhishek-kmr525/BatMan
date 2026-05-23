"""Binance Spot live trading adapter.

Resilient behavior:
- Uses BINANCE_HOST when provided.
- Falls back across official Binance hosts on DNS/network failures.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
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

_clients: dict[str, httpx.AsyncClient] = {}
_healthy_base: str | None = None


def _candidate_bases() -> list[str]:
    if settings.BINANCE_USE_TESTNET:
        return ["https://testnet.binance.vision"]
    out: list[str] = []
    host = (settings.BINANCE_HOST or "").strip().rstrip("/")
    if host:
        out.append(host)
    for b in _DEFAULT_BASES:
        if b not in out:
            out.append(b)
    if _healthy_base and _healthy_base in out:
        out.remove(_healthy_base)
        out.insert(0, _healthy_base)
    return out


async def _get_client(base_url: str) -> httpx.AsyncClient:
    c = _clients.get(base_url)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": "amta-candle-bot/1.0"},
        )
        _clients[base_url] = c
    return c


def _has_creds() -> bool:
    return bool(settings.BINANCE_API_KEY and settings.BINANCE_API_SECRET)


def _sign(params: dict) -> dict:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10_000
    query = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(
        settings.BINANCE_API_SECRET.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = sig
    return params


async def _signed_request(method: str, path: str, params: dict | None = None) -> dict:
    if not _has_creds():
        raise RuntimeError("Binance API credentials not set (BINANCE_API_KEY/SECRET)")

    signed = _sign(params or {})
    headers = {"X-MBX-APIKEY": settings.BINANCE_API_KEY}

    last_error: Exception | None = None
    global _healthy_base
    for base in _candidate_bases():
        client = await _get_client(base)
        try:
            if method.upper() == "GET":
                res = await client.get(path, params=signed, headers=headers)
            elif method.upper() == "POST":
                res = await client.post(path, params=signed, headers=headers)
            elif method.upper() == "DELETE":
                res = await client.delete(path, params=signed, headers=headers)
            else:
                raise ValueError(f"unsupported method: {method}")

            if res.status_code >= 400:
                try:
                    err = res.json()
                except Exception:
                    err = {"msg": res.text[:200]}
                raise RuntimeError(f"Binance {method} {path} HTTP {res.status_code}: {err}")

            _healthy_base = base
            return res.json()
        except Exception as e:
            last_error = e
            logger.warning("Binance signed request failed via %s %s %s: %s", base, method, path, e)
            continue

    raise RuntimeError(str(last_error) if last_error else "Binance request failed")


async def _public_request(path: str, params: dict | None = None) -> dict:
    last_error: Exception | None = None
    global _healthy_base
    for base in _candidate_bases():
        client = await _get_client(base)
        try:
            res = await client.get(path, params=params or {})
            res.raise_for_status()
            _healthy_base = base
            return res.json()
        except Exception as e:
            last_error = e
            logger.warning("Binance public request failed via %s %s: %s", base, path, e)
            continue
    raise RuntimeError(str(last_error) if last_error else "Binance public request failed")


async def get_account_balance() -> tuple[float, dict]:
    data = await _signed_request("GET", "/api/v3/account")
    balances = {
        b["asset"]: {"free": float(b["free"]), "locked": float(b["locked"])}
        for b in data.get("balances", [])
    }
    usdt_free = balances.get("USDT", {}).get("free", 0.0)
    return usdt_free, balances


async def get_account_balance_safe() -> tuple[float, dict, str | None]:
    if not _has_creds():
        return (0.0, {}, "Binance API credentials not configured")
    try:
        bal, full = await get_account_balance()
        return (bal, full, None)
    except Exception as e:
        return (0.0, {}, str(e)[:300])


_SYMBOL_FILTERS: dict[str, dict] = {}


async def get_symbol_filters(symbol: str) -> dict:
    symbol = symbol.upper()
    if symbol in _SYMBOL_FILTERS:
        return _SYMBOL_FILTERS[symbol]
    try:
        data = await _public_request("/api/v3/exchangeInfo", params={"symbol": symbol})
    except Exception as e:
        logger.warning("exchangeInfo fetch failed for %s: %s", symbol, e)
        return {}

    symbols = data.get("symbols", [])
    if not symbols:
        return {}
    s = symbols[0]
    filters = {f["filterType"]: f for f in s.get("filters", [])}
    out = {
        "baseAsset": s.get("baseAsset"),
        "quoteAsset": s.get("quoteAsset"),
        "tickSize": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.01)),
        "stepSize": float(filters.get("LOT_SIZE", {}).get("stepSize", 0.0001)),
        "minQty": float(filters.get("LOT_SIZE", {}).get("minQty", 0.0001)),
        "minNotional": float(
            filters.get("NOTIONAL", {}).get("minNotional")
            or filters.get("MIN_NOTIONAL", {}).get("minNotional")
            or 5.0
        ),
    }
    _SYMBOL_FILTERS[symbol] = out
    return out


def _round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    n = int(qty / step)
    return round(n * step, 10)


async def place_market_buy(symbol: str, quote_usd: float) -> dict:
    if not _has_creds():
        return {"ok": False, "error": "no credentials"}
    try:
        filters = await get_symbol_filters(symbol)
        min_notional = filters.get("minNotional", 5.0)
        if quote_usd < min_notional:
            return {"ok": False, "error": f"quote_usd ${quote_usd:.2f} below min notional ${min_notional}"}
        params = {
            "symbol": symbol.upper(),
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": round(quote_usd, 2),
            "newOrderRespType": "FULL",
        }
        data = await _signed_request("POST", "/api/v3/order", params)
        executed_qty = float(data.get("executedQty", 0))
        cum_quote = float(data.get("cummulativeQuoteQty", 0))
        avg_price = (cum_quote / executed_qty) if executed_qty > 0 else 0.0
        return {
            "ok": True,
            "orderId": data.get("orderId"),
            "executed_qty": executed_qty,
            "executed_price": round(avg_price, 8),
            "cum_quote_usd": cum_quote,
            "status": data.get("status"),
            "raw": data,
        }
    except Exception as e:
        logger.error("binance market BUY %s failed: %s", symbol, e)
        return {"ok": False, "error": str(e)[:300]}


async def place_market_sell(symbol: str, qty: float) -> dict:
    if not _has_creds():
        return {"ok": False, "error": "no credentials"}
    try:
        filters = await get_symbol_filters(symbol)
        step = filters.get("stepSize", 0.0001)
        qty = _round_step(qty, step)
        if qty < filters.get("minQty", 0.0001):
            return {"ok": False, "error": f"qty {qty} below min lot {filters.get('minQty')}"}
        params = {
            "symbol": symbol.upper(),
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty,
            "newOrderRespType": "FULL",
        }
        data = await _signed_request("POST", "/api/v3/order", params)
        executed_qty = float(data.get("executedQty", 0))
        cum_quote = float(data.get("cummulativeQuoteQty", 0))
        avg_price = (cum_quote / executed_qty) if executed_qty > 0 else 0.0
        return {
            "ok": True,
            "orderId": data.get("orderId"),
            "executed_qty": executed_qty,
            "executed_price": round(avg_price, 8),
            "cum_quote_usd": cum_quote,
            "status": data.get("status"),
            "raw": data,
        }
    except Exception as e:
        logger.error("binance market SELL %s failed: %s", symbol, e)
        return {"ok": False, "error": str(e)[:300]}


async def get_open_orders(symbol: str | None = None) -> list[dict]:
    if not _has_creds():
        return []
    try:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await _signed_request("GET", "/api/v3/openOrders", params)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("open orders fetch failed: %s", e)
        return []


async def close():
    for base, c in list(_clients.items()):
        if c and not c.is_closed:
            await c.aclose()
        _clients.pop(base, None)
