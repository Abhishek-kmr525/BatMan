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
)
from py_clob_client_v2.order_builder.constants import BUY
from app.core.config import settings

logger = logging.getLogger(__name__)

_client: ClobClient | None = None
_proxy_installed: bool = False


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
            sig_type = int(getattr(settings, "POLYMARKET_SIGNATURE_TYPE", 2))
            signer = getattr(settings, "POLYMARKET_SIGNER_ADDRESS", None) or os.getenv("POLYMARKET_SIGNER_ADDRESS", "")
            wallet = getattr(settings, "POLYMARKET_WALLET_ADDRESS", None) or os.getenv("POLYMARKET_WALLET_ADDRESS", "")
            is_eoa = bool(signer and wallet and signer.lower() == wallet.lower())
            # For Gnosis Safe (sig_type=2) the funder must be the Safe address.
            # Prefer explicit POLYMARKET_FUNDER_ADDRESS; fall back to
            # POLYMARKET_WALLET_ADDRESS (which IS the Safe address when the
            # signer ≠ wallet, i.e. the relayer key signs on behalf of the Safe).
            if is_eoa:
                funder_addr = None
            else:
                funder_addr = (
                    getattr(settings, "POLYMARKET_FUNDER_ADDRESS", None)
                    or getattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
                    or os.getenv("POLYMARKET_WALLET_ADDRESS", "")
                ) or None

            _client = ClobClient(
                host=settings.POLYMARKET_HOST,
                key=settings.POLYMARKET_PRIVATE_KEY,
                chain_id=settings.POLYMARKET_CHAIN_ID,
                funder=funder_addr,
                signature_type=sig_type,
            )
            logger.info(f"Polymarket ClobClient initialised (signature_type={sig_type})")
            try:
                creds = _client.create_api_key()
            except Exception:
                creds = _client.derive_api_key()
            _client.set_api_creds(creds)
        except Exception as e:
            logger.error(f"Failed to init Polymarket ClobClient: {e}")
            return None
    return _client

_last_balance_error: str = ""


async def get_live_balance() -> float:
    """Return live USDC balance, or 0.0 on failure (error stored in get_live_balance_error())."""
    global _last_balance_error
    _last_balance_error = ""
    client = get_live_client()
    if not client:
        _last_balance_error = "ClobClient not initialised — check POLYMARKET_PRIVATE_KEY"
        return 0.0
    try:
        res = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        # Balance returned as raw USDC atomic units (6 decimals); divide by 1e6.
        raw_bal = float(res.get("balance", 0))
        return raw_bal / 1e6
    except Exception as e:
        err_msg = str(getattr(e, "error_msg", getattr(e, "error_message", str(e))))
        if hasattr(e, "status_code"):
            err_msg = f"HTTP {e.status_code}: {err_msg}"
        _last_balance_error = err_msg
        logger.error(f"Failed to fetch live polymarket balance: {err_msg}")
        return 0.0


def get_live_balance_error() -> str:
    """Return the last error string from get_live_balance(), or empty string if OK."""
    return _last_balance_error

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

def _fallback_order_placement(original_client, order_args, neg_risk):
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
                    # Update module-level client so future orders use the working config.
                    global _client
                    _client = fallback_client
                    return {"ok": True, "orderID": resp.get("orderID")}
            except Exception as fe:
                err_str = str(getattr(fe, "error_msg", getattr(fe, "error_message", str(fe))))
                logger.info(f"Fallback ({funder}/{sig_type}) → {err_str[:80]}")
                continue
                
        return {"ok": False, "error": "All signature fallbacks failed."}
    except Exception as e:
        logger.error(f"Fallback process crashed: {e}")
        return {"ok": False, "error": "Fallback logic failed."}
