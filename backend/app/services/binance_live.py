"""Binance Spot live trading adapter.

Uses Binance's REST API directly with HMAC-SHA256 signed requests.
No external SDK required — pure stdlib + httpx.

Endpoints used:
  - GET  /api/v3/account                  → balance
  - POST /api/v3/order                    → place market/limit order
  - GET  /api/v3/order                    → order status
  - DELETE /api/v3/order                  → cancel order
  - GET  /api/v3/exchangeInfo             → symbol filters (qty step, price tick)
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


def _base_url() -> str:
    if settings.BINANCE_USE_TESTNET:
        return "https://testnet.binance.vision"
    # Allow routing through a relay when Binance is geo-blocked.
    return settings.BINANCE_HOST or "https://api.binance.com"


_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=_base_url(),
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": "amta-candle-bot/1.0"},
        )
    return _client


def _has_creds() -> bool:
    return bool(settings.BINANCE_API_KEY and settings.BINANCE_API_SECRET)


def _sign(params: dict) -> dict:
    """Add timestamp + HMAC signature to params (Binance signed-request standard)."""
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
    """Make a signed Binance request — raises on non-2xx."""
    if not _has_creds():
        raise RuntimeError("Binance API credentials not set (BINANCE_API_KEY/SECRET)")
    client = await _get_client()
    signed = _sign(params or {})
    headers = {"X-MBX-APIKEY": settings.BINANCE_API_KEY}
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
    return res.json()


# ─────────────────────────── ACCOUNT ─────────────────────────────────

async def get_account_balance() -> tuple[float, dict]:
    """Return (free USDT balance, full balances dict).

    Raises if credentials missing or request fails.
    """
    data = await _signed_request("GET", "/api/v3/account")
    balances = {b["asset"]: {"free": float(b["free"]), "locked": float(b["locked"])} for b in data.get("balances", [])}
    usdt_free = balances.get("USDT", {}).get("free", 0.0)
    return (usdt_free, balances)


async def get_account_balance_safe() -> tuple[float, dict, str | None]:
    """Same as get_account_balance but returns error string instead of raising."""
    if not _has_creds():
        return (0.0, {}, "Binance API credentials not configured")
    try:
        bal, full = await get_account_balance()
        return (bal, full, None)
    except Exception as e:
        return (0.0, {}, str(e)[:300])


# ─────────────────────── SYMBOL FILTERS ──────────────────────────────

_SYMBOL_FILTERS: dict[str, dict] = {}


async def get_symbol_filters(symbol: str) -> dict:
    """Get tickSize, stepSize, minNotional for a symbol (cached)."""
    symbol = symbol.upper()
    if symbol in _SYMBOL_FILTERS:
        return _SYMBOL_FILTERS[symbol]
    client = await _get_client()
    try:
        res = await client.get("/api/v3/exchangeInfo", params={"symbol": symbol})
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        logger.warning(f"exchangeInfo fetch failed for {symbol}: {e}")
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
    """Round qty DOWN to the nearest valid step size."""
    if step <= 0:
        return qty
    n = int(qty / step)
    return round(n * step, 10)


# ─────────────────────────── ORDERS ──────────────────────────────────

async def place_market_buy(symbol: str, quote_usd: float) -> dict:
    """Place a MARKET BUY order using USD quote amount (Binance quoteOrderQty).

    Returns: {ok, orderId, executed_qty, executed_price, status, raw}
    """
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
        fills = data.get("fills", [])
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
        logger.error(f"binance market BUY {symbol} failed: {e}")
        return {"ok": False, "error": str(e)[:300]}


async def place_market_sell(symbol: str, qty: float) -> dict:
    """Place a MARKET SELL order with base asset quantity."""
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
        logger.error(f"binance market SELL {symbol} failed: {e}")
        return {"ok": False, "error": str(e)[:300]}


async def get_open_orders(symbol: str | None = None) -> list[dict]:
    """List currently open orders (optionally filtered by symbol)."""
    if not _has_creds():
        return []
    try:
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await _signed_request("GET", "/api/v3/openOrders", params)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"open orders fetch failed: {e}")
        return []


async def close():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
