"""Kalshi API v2 client.

Auth (when KALSHI_DEMO=false):
  Per-request RSA-PSS-SHA256 signing per Kalshi docs.
  Headers:
    KALSHI-ACCESS-KEY:       <key id>
    KALSHI-ACCESS-TIMESTAMP: <ms since epoch>
    KALSHI-ACCESS-SIGNATURE: base64( RSA-PSS-SHA256( ts + METHOD + path ) )

  `path` is the URL path component including the API prefix
  (e.g. /trade-api/v2/markets), without query string.

Demo mode generates a stable mock universe of markets so the rest of the
system runs without credentials.
"""
from __future__ import annotations

import asyncio
import base64
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings


@dataclass
class Market:
    ticker: str
    title: str
    category: str
    yes_price: float  # 0..1
    no_price: float  # 0..1
    volume: int
    open_interest: int
    close_time_seconds: int
    raw: dict[str, Any]


# ------------------------- Mock universe (demo mode) ------------------------
_MOCK_CATEGORIES = [
    "Politics", "Economics", "Sports", "Weather", "Crypto", "Culture", "Science", "World",
]
_MOCK_TITLES = [
    "Will BTC close above $70k this week?",
    "Will the Fed cut rates in May?",
    "Will Lakers win tonight?",
    "Will it rain in NYC tomorrow?",
    "Will CPI print above 3.0%?",
    "Will the S&P close green Friday?",
    "Will SpaceX launch on schedule?",
    "Will the new film top $100M opening?",
]


def _mock_price_for(ticker: str) -> float:
    base = abs(hash(ticker)) % 100 / 100.0
    drift = (int(time.time()) // 15) % 20 / 200.0
    p = base + drift
    if p > 0.98:
        p -= 0.5
    return round(max(0.02, min(0.98, p)), 2)


def _mock_universe(n: int = 30) -> list[Market]:
    rng = random.Random(42)
    out: list[Market] = []
    for i in range(n):
        cat = rng.choice(_MOCK_CATEGORIES)
        title = rng.choice(_MOCK_TITLES) + f" #{i}"
        ticker = f"MOCK-{cat[:3].upper()}-{i:03d}"
        out.append(Market(
            ticker=ticker,
            title=title,
            category=cat,
            yes_price=_mock_price_for(ticker),
            no_price=round(1.0 - _mock_price_for(ticker), 2),
            volume=rng.randint(100, 50_000),
            open_interest=rng.randint(50, 20_000),
            close_time_seconds=rng.randint(300, 60 * 60 * 48),
            raw={"mock": True},
        ))
    return out


# Series tickers known to host liquid markets. We hit each one directly
# instead of paginating past 8000+ stale parlay markets.
LIQUID_SERIES = [
    # Energy
    "KXWTI", "KXBRENT", "KXBRENTD", "KXNATGAS",
    # Sports — game/match level
    "KXMLBGAME", "KXNBAGAME", "KXNFLGAME", "KXNHLGAME",
    "KXATPMATCH", "KXATPCHALLENGERMATCH", "KXWTAMATCH",
    # Sports — player props
    "KXMLBHIT", "KXMLBHR", "KXMLBKS", "KXMLBRBI",
    "KXNBAPOINTS", "KXNBAREB", "KXNBAAST",
    # Macro
    "KXFEDDECISION", "KXCPI", "KXCPIYOY", "KXPCE", "KXJOBS",
    "KXGDP", "KXUNEMP",
    # Markets
    "KXSPX", "KXNDX", "KXVIX", "KXBTCD", "KXETHD", "KXSOLD",
    # Politics / culture
    "KXPRESPRIM", "KXPRESELEC", "KXOSCAR",
]


# --------------------------- RSA-PSS signing -------------------------------
_private_key = None


def _load_private_key():
    global _private_key
    if _private_key is None:
        from cryptography.hazmat.primitives import serialization

        # Prefer env-delivered key material in production (single-line base64).
        if settings.KALSHI_PRIVATE_KEY_B64:
            key_bytes = base64.b64decode(settings.KALSHI_PRIVATE_KEY_B64)
            _private_key = serialization.load_pem_private_key(key_bytes, password=None)
            return _private_key

        path = Path(settings.KALSHI_PRIVATE_KEY_PATH)
        if not path.is_absolute():
            # resolve relative to backend/ (project working dir at runtime)
            path = (Path.cwd() / path).resolve()
        with open(path, "rb") as f:
            _private_key = serialization.load_pem_private_key(f.read(), password=None)
    return _private_key


def _sign(method: str, path: str) -> tuple[str, str]:
    """Return (timestamp_ms_str, base64_signature)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode("utf-8")
    sig = _load_private_key().sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode("ascii")


# ------------------------------- Client ------------------------------------
class KalshiClient:
    def __init__(self) -> None:
        # Demo mode is an explicit switch. When false, we can still use
        # public market-data endpoints without API credentials.
        self.demo = settings.KALSHI_DEMO
        self.base_url = settings.KALSHI_BASE_URL.rstrip("/")
        self._http = httpx.AsyncClient(timeout=20.0)
        # The signed `path` in Kalshi auth is the full URL path
        # including the /trade-api/v2 prefix.
        self._url_path_prefix = urlparse(self.base_url).path  # e.g. /trade-api/v2
        # In-memory cache for the (slow) liquid-market scan.
        self._market_cache: tuple[float, list[Market]] | None = None
        self._market_cache_ttl = 300.0  # 5 min — cache the slow paginate
        # Short-lived per-ticker cache to reduce repeated /markets/{ticker} hits.
        self._ticker_cache: dict[str, tuple[float, Market | None]] = {}
        self._ticker_cache_ttl = 8.0

    async def close(self) -> None:
        await self._http.aclose()

    # ---- low-level signed request ----
    async def _request(
        self,
        method: str,
        suffix: str,
        *,
        require_auth: bool = False,
        **kw,
    ) -> httpx.Response:
        url = f"{self.base_url}{suffix}"
        headers = {
            "Content-Type": "application/json",
            "accept": "application/json",
        }
        if require_auth:
            if not settings.KALSHI_KEY_ID:
                raise RuntimeError("KALSHI_KEY_ID is required for authenticated requests")
            signed_path = f"{self._url_path_prefix}{suffix.split('?')[0]}"
            ts, sig = _sign(method, signed_path)
            headers["KALSHI-ACCESS-KEY"] = settings.KALSHI_KEY_ID
            headers["KALSHI-ACCESS-TIMESTAMP"] = ts
            headers["KALSHI-ACCESS-SIGNATURE"] = sig
        attempts = 0
        max_attempts = 4
        while True:
            attempts += 1
            resp = await self._http.request(method, url, headers=headers, **kw)
            if resp.status_code != 429 or attempts >= max_attempts:
                return resp
            await asyncio.sleep(_retry_after_seconds(resp.headers.get("retry-after")))

    # ---- markets ----
    async def get_markets(
        self,
        limit: int = 200,
        min_volume: int = 0,
        max_pages: int = 10,
        use_cache: bool = True,
    ) -> list[Market]:
        """Fetch open markets. When min_volume>0, paginates until we
        accumulate `limit` liquid markets (or hit max_pages).

        Result is cached per (limit, min_volume) for self._market_cache_ttl
        seconds so a 30s scan loop doesn't repaginate the whole universe."""
        if self.demo:
            return _mock_universe(min(limit, 30))

        if use_cache and self._market_cache is not None:
            ts, cached = self._market_cache
            if time.time() - ts < self._market_cache_ttl:
                return cached[:limit]

        per_page = 1000 if min_volume > 0 else min(limit, 1000)
        out: list[Market] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": per_page, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            r = await self._request("GET", "/markets", params=params)
            r.raise_for_status()
            data = r.json()
            raw = data.get("markets", [])
            # Drop auto-generated multivariate-event parlays — they never trade.
            raw = [r for r in raw if not _is_parlay(r)]
            page = [_parse_market(m) for m in raw]
            if min_volume > 0:
                page = [m for m in page if m.volume >= min_volume]
            out.extend(page)
            cursor = data.get("cursor") or None
            if not cursor or len(out) >= limit:
                break
        result = out[:limit]
        if use_cache:
            self._market_cache = (time.time(), result)
        return result

    async def _series_markets(self, series_ticker: str) -> list[Market]:
        """Fetch all open markets in one series. Returns []
        on any error (so one bad series doesn't break the whole sweep)."""
        try:
            r = await self._request(
                "GET", "/markets",
                params={"series_ticker": series_ticker, "limit": 200, "status": "open"},
            )
            if r.status_code != 200:
                return []
            raw = r.json().get("markets", []) or []
            raw = [x for x in raw if not _is_parlay(x)]
            return [_parse_market(x) for x in raw]
        except Exception:
            return []

    async def get_liquid_markets(
        self,
        limit: int = 120,
        min_volume: int = 10,
        series: list[str] | None = None,
        use_cache: bool = True,
    ) -> list[Market]:
        """Fetch markets across known-liquid series in parallel.

        On a warm Kalshi this returns in 1–3 s vs 60–160 s for the
        cursor-paginated path that has to walk past 8 000+ dead markets."""
        if self.demo:
            return _mock_universe(min(limit, 30))

        if use_cache and self._market_cache is not None:
            ts, cached = self._market_cache
            if time.time() - ts < self._market_cache_ttl:
                return cached[:limit]

        targets = series or LIQUID_SERIES
        results = await asyncio.gather(
            *(self._series_markets(s) for s in targets),
            return_exceptions=False,
        )
        flat: list[Market] = []
        for batch in results:
            flat.extend(batch)
        if min_volume > 0:
            flat = [m for m in flat if m.volume >= min_volume]
        flat.sort(key=lambda m: m.volume, reverse=True)
        out = flat[:limit]
        if use_cache:
            self._market_cache = (time.time(), out)
        return out

    async def get_market(self, ticker: str) -> Market | None:
        if self.demo:
            for m in _mock_universe(30):
                if m.ticker == ticker:
                    m.yes_price = _mock_price_for(ticker)
                    m.no_price = round(1.0 - m.yes_price, 2)
                    return m
            return None
        now = time.time()
        cached = self._ticker_cache.get(ticker)
        if cached and now - cached[0] < self._ticker_cache_ttl:
            return cached[1]
        r = await self._request("GET", f"/markets/{ticker}")
        if r.status_code == 404:
            self._ticker_cache[ticker] = (now, None)
            return None
        r.raise_for_status()
        market = _parse_market(r.json().get("market", {}))
        self._ticker_cache[ticker] = (now, market)
        return market

    async def current_price(self, ticker: str, side: str) -> float | None:
        m = await self.get_market(ticker)
        if not m:
            return None
        return m.yes_price if side.upper() == "YES" else m.no_price

    # ---- orders ----
    async def place_order(
        self,
        ticker: str,
        action: str,  # buy | sell
        side: str,    # yes | no
        count: int = 1,
        price_cents: int | None = None,
    ) -> dict[str, Any]:
        if self.demo:
            return {
                "order_id": f"mock-{int(time.time()*1000)}",
                "ticker": ticker,
                "action": action,
                "side": side,
                "count": count,
                "filled_price_cents": price_cents or int(_mock_price_for(ticker) * 100),
                "status": "filled",
            }
        if settings.KALSHI_PAPER_MODE:
            # Real-data, paper-fills: never send order to Kalshi.
            # Use the live current price as the synthetic fill.
            cur = await self.current_price(ticker, side)
            fill_cents = int(round((cur or 0.5) * 100))
            return {
                "order_id": f"paper-{int(time.time()*1000)}",
                "ticker": ticker,
                "action": action,
                "side": side,
                "count": count,
                "filled_price_cents": fill_cents,
                "status": "filled",
                "paper": True,
            }
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": "limit" if price_cents is not None else "market",
            "client_order_id": f"amta-{int(time.time()*1000)}",
        }
        if price_cents is not None:
            body[f"{side}_price"] = int(max(1, min(99, price_cents)))
        r = await self._request("POST", "/portfolio/orders", json=body, require_auth=True)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Kalshi order error {r.status_code}: {r.text}",
                request=r.request, response=r,
            )
        return r.json()

    async def get_positions(self) -> list[dict[str, Any]]:
        if self.demo:
            return []
        r = await self._request("GET", "/portfolio/positions", require_auth=True)
        r.raise_for_status()
        data = r.json()
        return data.get("market_positions", []) or []

    async def get_balance(self) -> dict[str, Any]:
        if self.demo:
            return {"balance": 0, "demo": True}
        r = await self._request("GET", "/portfolio/balance")
        r.raise_for_status()
        return r.json()


# ------------------------------- Parsers -----------------------------------
_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("KXWTI", "Oil"),
    ("KXBRENT", "Oil"),
    ("KXNATGAS", "Energy"),
    ("KXMLB", "MLB"),
    ("KXNBA", "NBA"),
    ("KXNFL", "NFL"),
    ("KXNHL", "NHL"),
    ("KXATP", "Tennis"),
    ("KXWTA", "Tennis"),
    ("KXUSOPEN", "Tennis"),
    ("KXWIMBLEDON", "Tennis"),
    ("KXFED", "Macro"),
    ("KXCPI", "Macro"),
    ("KXPCE", "Macro"),
    ("KXGDP", "Macro"),
    ("KXJOBS", "Macro"),
    ("KXBTC", "Crypto"),
    ("KXETH", "Crypto"),
    ("KXSOL", "Crypto"),
    ("KXSPX", "Markets"),
    ("KXNDX", "Markets"),
    ("KXVIX", "Markets"),
    ("KXPRES", "Politics"),
    ("KXSENATE", "Politics"),
    ("KXHOUSE", "Politics"),
    ("KXELEC", "Politics"),
    ("KXWEATHER", "Weather"),
    ("KXTEMP", "Weather"),
    ("KXSNOW", "Weather"),
    ("KXOSCAR", "Culture"),
    ("KXBOX", "Culture"),
]


def _category_from_ticker(ticker: str) -> str:
    if not ticker:
        return ""
    t = ticker.upper()
    for prefix, label in _CATEGORY_PREFIXES:
        if t.startswith(prefix):
            return label
    return "Other"


def _parse_market(m: dict[str, Any]) -> Market:
    """Parse a Kalshi market response. As of 2026, Kalshi uses *_dollars
    (0..1) and *_fp suffixes; older fields (yes_bid in cents, volume) are
    accepted as fallback for forward compatibility."""

    def _f(*keys, default=None):
        for k in keys:
            v = m.get(k)
            if v not in (None, "", 0, 0.0):
                return v
        return default

    # Prices: prefer dollars; fall back to cents
    yb = _f("yes_bid_dollars")
    ya = _f("yes_ask_dollars")
    if yb is not None and ya is not None:
        yes = (float(yb) + float(ya)) / 2.0
    elif _f("last_price_dollars") is not None:
        yes = float(_f("last_price_dollars"))
    elif m.get("yes_bid") is not None and m.get("yes_ask") is not None:
        yes = (float(m["yes_bid"]) + float(m["yes_ask"])) / 200.0  # cents->dollars
    elif m.get("last_price") is not None:
        yes = float(m["last_price"]) / 100.0
    else:
        yes = 0.5
    yes = max(0.01, min(0.99, yes))

    # Volume + OI (new fields are *_fp)
    volume = int(float(_f("volume_24h_fp", "volume_fp", "volume", default=0) or 0))
    oi = int(float(_f("open_interest_fp", "open_interest", default=0) or 0))

    close_secs = 3600
    ct = m.get("close_time")
    if isinstance(ct, str):
        try:
            t = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            delta = t - datetime.now(timezone.utc)
            close_secs = max(0, int(delta.total_seconds()))
        except Exception:
            pass

    ticker = m.get("ticker", "")
    return Market(
        ticker=ticker,
        title=m.get("title") or m.get("yes_sub_title") or ticker,
        category=m.get("category") or _category_from_ticker(ticker),
        yes_price=round(yes, 2),
        no_price=round(1 - yes, 2),
        volume=volume,
        open_interest=oi,
        close_time_seconds=close_secs,
        raw=m,
    )


def _is_parlay(m: dict[str, Any]) -> bool:
    """Multivariate-event markets are auto-generated parlays that rarely trade."""
    if m.get("mve_collection_ticker"):
        return True
    t = m.get("ticker", "")
    return t.startswith("KXMVE") or t.startswith("KXMV")


_kalshi: KalshiClient | None = None


def get_kalshi() -> KalshiClient:
    global _kalshi
    if _kalshi is None:
        _kalshi = KalshiClient()
    return _kalshi


def reset_kalshi() -> None:
    global _kalshi
    _kalshi = None


def _retry_after_seconds(raw: str | None) -> float:
    if not raw:
        return 1.2
    try:
        return max(0.2, min(float(raw), 8.0))
    except Exception:
        return 1.2
