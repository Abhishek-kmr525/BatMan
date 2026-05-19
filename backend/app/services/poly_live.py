"""Live trading adapter for Polymarket (phase-3)."""
from __future__ import annotations

import logging
import httpx
from py_clob_client_v2 import http_helpers as _v2_http_helpers
from py_clob_client_v2.http_helpers import helpers as _v2_helpers_mod
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    BalanceAllowanceParams,
    AssetType,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    ApiCreds,
)
from py_clob_client_v2.order_builder.constants import BUY
from app.core.config import settings

logger = logging.getLogger(__name__)

_client: ClobClient | None = None
_proxy_installed: bool = False
_last_balance_error_ts: float | None = None
_last_balance_error_kind: str = ""

def _hydrate_api_creds(client: ClobClient) -> bool:
    """Ensure client has L2 creds; retry-safe for long-lived singleton client."""
    try:
        # Already hydrated.
        if getattr(client, "creds", None):
            return True
    except Exception:
        pass

    import os
    api_key = (
        getattr(settings, "POLYMARKET_API_KEY", None)
        or os.getenv("POLYMARKET_API_KEY", "")
    ) or ""
    api_secret = (
        getattr(settings, "POLYMARKET_API_SECRET", None)
        or os.getenv("POLYMARKET_API_SECRET", "")
    ) or ""
    api_passphrase = (
        getattr(settings, "POLYMARKET_API_PASSPHRASE", None)
        or os.getenv("POLYMARKET_API_PASSPHRASE", "")
    ) or ""

    # Prefer explicit env creds when present.
    if api_key and api_secret and api_passphrase:
        try:
            client.set_api_creds(
                ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                )
            )
            logger.info("Hydrated CLOB API creds from env")
            return True
        except Exception as e:
            logger.warning(f"Failed to set API creds from env: {e}")

    # Otherwise derive from signer.
    for method in ("create_or_derive_api_key", "create_api_key", "derive_api_key"):
        fn = getattr(client, method, None)
        if fn is None:
            continue
        try:
            creds = fn()
            if creds is not None:
                client.set_api_creds(creds)
                logger.info(f"Hydrated CLOB API creds via {method}")
                return True
        except Exception as e:
            logger.warning(f"{method} failed: {e}")
    return False


def _install_proxy_if_configured() -> None:
    """Route only Polymarket CLOB traffic through a configured outbound proxy.

    The v2 SDK uses a module-level `httpx.Client` for all CLOB calls. Replacing
    it here keeps OpenAI, Gamma, and other outbound traffic on the direct path
    while sending CLOB requests through a proxy in an allowed region — needed
    when the host's egress IP is geo-blocked by Polymarket.
    """
    global _proxy_installed
    if _proxy_installed:
        return
    proxy_url = getattr(settings, "POLYMARKET_PROXY_URL", "") or ""
    host_url = getattr(settings, "POLYMARKET_HOST", "") or ""
    if not proxy_url or "trycloudflare.com" in host_url:
        return
    try:
        new_client = httpx.Client(http2=True, proxy=proxy_url, timeout=30.0)
        old_client = getattr(_v2_helpers_mod, "_http_client", None)
        _v2_helpers_mod._http_client = new_client
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
        _proxy_installed = True
        logger.info(
            f"Polymarket CLOB HTTP client routed via proxy "
            f"{proxy_url.split('@')[-1]}"
        )
    except Exception as e:
        logger.error(f"Failed to install Polymarket proxy: {e}")

def get_live_client() -> ClobClient | None:
    global _client
    if not settings.POLYMARKET_PRIVATE_KEY:
        return None
    _install_proxy_if_configured()
    if _client is None:
        try:
            import os
            # Always use explicit env config — the fallback sweeper discovers the
            # working (sig_type, funder) and we lock it in via env vars.
            sig_type = int(getattr(settings, "POLYMARKET_SIGNATURE_TYPE", 0))
            explicit_funder = (
                getattr(settings, "POLYMARKET_FUNDER_ADDRESS", None)
                or os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
            ) or None
            funder_addr = explicit_funder or (
                (getattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
                 or os.getenv("POLYMARKET_WALLET_ADDRESS", "")) or None
            ) if sig_type != 0 else explicit_funder

            _client = ClobClient(
                host=settings.POLYMARKET_HOST,
                key=settings.POLYMARKET_PRIVATE_KEY,
                chain_id=settings.POLYMARKET_CHAIN_ID,
                funder=funder_addr,
                signature_type=sig_type,
            )
            logger.info(
                f"Polymarket ClobClient initialised "
                f"(signature_type={sig_type}, funder={funder_addr})"
            )

            # Best-effort initial hydration; get_live_balance/place_live_order
            # will retry hydration on demand if this step fails.
            _hydrate_api_creds(_client)
        except Exception as e:
            logger.error(f"Failed to init Polymarket ClobClient: {e}")
            return None
    return _client

_last_balance_error: str = ""


async def get_live_balance() -> float:
    """Return live USDC balance, or 0.0 on failure (error stored in get_live_balance_error())."""
    import asyncio
    global _last_balance_error, _last_balance_error_ts, _last_balance_error_kind
    _last_balance_error = ""
    _last_balance_error_kind = ""
    _last_balance_error_ts = None
    client = get_live_client()
    if not client:
        _last_balance_error = "ClobClient not initialised — check POLYMARKET_PRIVATE_KEY"
        return 0.0
    # Heal clients that were initialized without creds after transient auth error.
    _hydrate_api_creds(client)
    try:
        # Run the blocking SDK call in a thread so it doesn't block the event loop.
        res = await asyncio.to_thread(
            client.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
        )
        # Balance returned as raw USDC atomic units (6 decimals); divide by 1e6.
        raw_bal = float(res.get("balance", 0))
        return raw_bal / 1e6
    except Exception as e:
        err_msg = str(getattr(e, "error_msg", getattr(e, "error_message", str(e))))
        # Retry once if SDK says L2 creds are missing/invalid.
        if "API Credentials are needed" in err_msg:
            if _hydrate_api_creds(client):
                try:
                    res = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                    raw_bal = float(res.get("balance", 0))
                    return raw_bal / 1e6
                except Exception as e2:
                    err_msg = str(getattr(e2, "error_msg", getattr(e2, "error_message", str(e2))))
        if hasattr(e, "status_code"):
            err_msg = f"HTTP {e.status_code}: {err_msg}"
            _last_balance_error_kind = f"http_{getattr(e, 'status_code', 'error')}"
        else:
            low = err_msg.lower()
            if "name or service not known" in low or "nodename nor servname provided" in low:
                _last_balance_error_kind = "dns"
            elif "timed out" in low or "timeout" in low:
                _last_balance_error_kind = "timeout"
            elif "certificate" in low or "tls" in low or "ssl" in low:
                _last_balance_error_kind = "tls"
            elif "connect" in low:
                _last_balance_error_kind = "connect"
            else:
                _last_balance_error_kind = "request"
        _last_balance_error = err_msg
        _last_balance_error_ts = __import__("time").time()
        logger.error(
            "Failed to fetch live polymarket balance: %s | host=%s proxy=%s",
            err_msg,
            getattr(settings, "POLYMARKET_HOST", ""),
            "set" if bool(getattr(settings, "POLYMARKET_PROXY_URL", "")) else "empty",
        )
        return 0.0


def get_live_balance_error() -> str:
    """Return the last error string from get_live_balance(), or empty string if OK."""
    return _last_balance_error


def get_live_balance_error_meta() -> dict:
    return {
        "message": _last_balance_error or "",
        "kind": _last_balance_error_kind or "",
        "timestamp": _last_balance_error_ts,
    }


async def get_live_preflight(retries: int = 2) -> dict:
    """Docs-aligned live preflight checks for CLOB trading readiness."""
    import os
    host = getattr(settings, "POLYMARKET_HOST", "") or ""
    sig_type = int(getattr(settings, "POLYMARKET_SIGNATURE_TYPE", 0))
    funder = (
        getattr(settings, "POLYMARKET_FUNDER_ADDRESS", None)
        or os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
        or None
    )
    api_key = bool((getattr(settings, "POLYMARKET_API_KEY", None) or os.getenv("POLYMARKET_API_KEY", "")).strip())
    api_secret = bool((getattr(settings, "POLYMARKET_API_SECRET", None) or os.getenv("POLYMARKET_API_SECRET", "")).strip())
    api_pass = bool((getattr(settings, "POLYMARKET_API_PASSPHRASE", None) or os.getenv("POLYMARKET_API_PASSPHRASE", "")).strip())

    clob_reachable = False
    clob_status: int | None = None
    clob_error = ""
    for _ in range(max(1, retries)):
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
                r = await c.get(host)
                clob_status = r.status_code
                clob_reachable = True
                break
        except Exception as e:
            clob_error = str(e)
            await __import__("asyncio").sleep(0.35)

    client = get_live_client()
    client_ready = client is not None
    creds_ready = False
    if client is not None:
        creds_ready = _hydrate_api_creds(client)

    balance = await get_live_balance()
    err = get_live_balance_error()
    err_meta = get_live_balance_error_meta()
    balance_ok = (not err) and balance >= 0

    checks = {
        "clob_reachable": {"ok": clob_reachable, "status_code": clob_status, "error": clob_error or None},
        "auth_model": {"ok": client_ready, "l1_signer_present": bool(settings.POLYMARKET_PRIVATE_KEY), "l2_creds_ready": bool(creds_ready)},
        "funder_pairing": {"ok": bool(funder), "funder": funder, "signature_type": sig_type},
        "api_creds_status": {"ok": api_key and api_secret and api_pass, "api_key": api_key, "api_secret": api_secret, "api_passphrase": api_pass},
        "balance_fetch": {"ok": balance_ok, "balance": balance if balance_ok else None, "error": err or None, "error_kind": err_meta.get("kind"), "error_ts": err_meta.get("timestamp")},
    }
    allow_start = all(v.get("ok", False) for v in checks.values())
    return {
        "ok": allow_start,
        "host": host,
        "checks": checks,
        "resolved": {
            "funder": funder,
            "signature_type": sig_type,
            "client_ready": client_ready,
            "proxy_configured": bool(getattr(settings, "POLYMARKET_PROXY_URL", "")),
        },
    }

async def place_live_order(token_id: str, price: float, size: float, side: str) -> dict:
    """
    token_id: The asset token ID (YES or NO token ID).
    price: Entry price (e.g., 0.50)
    size: Number of contracts.
    side: "BUY" or "SELL".
    """
    client = get_live_client()
    if not client:
        return {"ok": False, "error": "live client not initialized"}
    _hydrate_api_creds(client)
    try:
        # Polymarket Up/Down crypto markets are negRisk. The CLOB order
        # typehash differs between standard and negRisk markets — the wrong
        # one fails with HTTP 400 order_version_mismatch. Detect per token.
        try:
            neg_risk = bool(client.get_neg_risk(token_id))
        except Exception as e:
            logger.warning(f"get_neg_risk failed for {token_id}: {e}; assuming False")
            neg_risk = False

        # tick_size also varies (0.01, 0.001, 0.0001). Round price to the
        # market's tick to avoid signature-mismatch rejections on the price
        # field. Default to 0.01 if lookup fails.
        try:
            tick_size = float(client.get_tick_size(token_id))
        except Exception as e:
            logger.warning(f"get_tick_size failed for {token_id}: {e}; using 0.01")
            tick_size = 0.01
        if tick_size > 0:
            price = round(round(price / tick_size) * tick_size, 6)

        order_args = OrderArgs(
            price=price,
            size=size,
            side=BUY if side.upper() == "BUY" else "SELL",
            token_id=token_id,
        )
        signed_order = client.create_order(
            order_args,
            options=PartialCreateOrderOptions(neg_risk=neg_risk),
        )
        resp = client.post_order(signed_order)

        if resp.get("success"):
            return {"ok": True, "orderID": resp.get("orderID")}
        else:
            err_msg = str(resp.get("errorMsg", "unknown error"))
            logger.error(f"Order failed with sig_type {client.builder.signature_type}: {err_msg}")
            
            # If it failed due to signature or maker issues, try falling back dynamically
            sig_errors = (
                "order_version_mismatch", "maker address not allowed",
                "invalid signature", "InvalidSignature",
            )
            if any(kw in str(err_msg) for kw in sig_errors):
                return _fallback_order_placement(client, order_args, neg_risk)

            return {"ok": False, "error": err_msg}

    except Exception as e:
        logger.error(f"Failed to place live polymarket order: {e}")
        err_msg = str(e)
        if hasattr(e, "status_code"):
            err_msg = str(getattr(e, "error_msg", getattr(e, "error_message", str(e))))
            logger.error(f"PolyApiException status={e.status_code} error={err_msg}")

            # Geo-block — no fallback will help; surface a clear message.
            if e.status_code == 403 and "Trading restricted" in str(err_msg):
                return {
                    "ok": False,
                    "error": "GEO_BLOCKED: Railway's US IP is blocked by Polymarket. "
                             "Set POLYMARKET_PROXY_URL in Railway env to a proxy in an allowed region.",
                }

            # Signature / maker mismatch — try sig-type fallbacks.
            sig_errors = (
                "order_version_mismatch", "maker address not allowed",
                "invalid signature", "InvalidSignature",
            )
            if any(kw in str(err_msg) for kw in sig_errors):
                return _fallback_order_placement(client, order_args, neg_risk)

        return {"ok": False, "error": err_msg}


async def cancel_live_order(order_id: str) -> dict:
    """Best-effort order cancel for resting live orders."""
    import asyncio
    client = get_live_client()
    if not client:
        return {"ok": False, "error": "live client not initialized"}
    _hydrate_api_creds(client)
    try:
        # SDK method names differ across releases; try common variants.
        for method in ("cancel", "cancel_order"):
            fn = getattr(client, method, None)
            if fn is None:
                continue
            res = await asyncio.to_thread(fn, order_id)
            if isinstance(res, dict):
                if res.get("success", True):
                    return {"ok": True, "result": res}
                return {"ok": False, "error": str(res.get("errorMsg") or res)}
            return {"ok": True, "result": res}
        return {"ok": False, "error": "cancel method not available in client"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def perform_live_withdraw(amount_usd: float, idempotency_key: str) -> tuple[bool, str | None, str | None]:
    """Best-effort live withdraw adapter.

    v1 ships with a safe placeholder: no transfer call is executed yet because
    CLOB SDK does not expose a direct collateral-withdraw endpoint in this app
    flow. Returns a clear message so the job pipeline and UI can be validated.
    """
    _ = (amount_usd, idempotency_key)
    return False, None, "withdraw adapter not configured"

def _fallback_order_placement(original_client, order_args, neg_risk):
    global _client  # declare once at top so inner assignments don't trigger SyntaxError
    logger.info("Attempting auto-fallback for signature_type and funder...")
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions
        import copy
        
        # Try every (funder, sig_type) combination in order of likelihood.
        # EOA (0) is most common for MetaMask/browser-wallet accounts.
        original_funder = original_client.builder.funder
        configs_to_try = [
            (None, 0),             # Raw EOA — most common for MetaMask accounts
            (original_funder, 1),  # Magic Proxy with configured funder
            (None, 1),             # Magic Proxy, no funder
            (original_funder, 2),  # Gnosis Safe with configured funder (current default)
            (None, 2),             # Gnosis Safe, no explicit funder
        ]
        
        for funder, sig_type in configs_to_try:
            if funder == original_funder and sig_type == original_client.builder.signature_type:
                continue # Already tried
                
            logger.info(f"Fallback attempt: funder={funder}, sig_type={sig_type}")
            try:
                fallback_client = ClobClient(
                    host=original_client.host,
                    key=original_client.builder.signer.private_key,
                    chain_id=original_client.chain_id,
                    funder=funder,
                    signature_type=sig_type,
                )
                # Re-derive API creds for this sig_type/funder combo —
                # creds are account-scoped so reusing originals would mismatch.
                try:
                    fb_creds = fallback_client.create_api_key()
                except Exception:
                    fb_creds = fallback_client.derive_api_key()
                fallback_client.set_api_creds(fb_creds)

                signed_order = fallback_client.create_order(
                    order_args,
                    options=PartialCreateOrderOptions(neg_risk=neg_risk),
                )
                resp = fallback_client.post_order(signed_order)
                if resp.get("success"):
                    logger.info(f"Fallback SUCCESS! funder={funder}, sig_type={sig_type}")
                    _client = fallback_client  # persist working config
                    return {"ok": True, "orderID": resp.get("orderID")}
                err_body = str(resp.get("errorMsg", resp))
                logger.info(f"Fallback ({funder}/{sig_type}) post: {err_body[:120]}")
                return {"ok": False, "error": err_body}
            except Exception as fe:
                err_str = str(getattr(fe, "error_msg", getattr(fe, "error_message", str(fe))))
                logger.info(f"Fallback ({funder}/{sig_type}) exc → {err_str[:120]}")

                # This config passed signature validation — persist it and
                # surface the real error (balance, size) so the bot can act.
                non_sig_errors = (
                    "not enough balance", "lower than the minimum",
                    "insufficient", "no liquidity",
                )
                if any(kw in err_str for kw in non_sig_errors):
                    _client = fallback_client  # persist working sig config
                    logger.info(
                        f"Persisting working sig config: funder={funder}, sig_type={sig_type}"
                    )
                    return {"ok": False, "error": err_str}
                continue

        return {"ok": False, "error": "All signature fallbacks failed."}
    except Exception as e:
        logger.error(f"Fallback process crashed: {e}")
        return {"ok": False, "error": "Fallback logic failed."}
