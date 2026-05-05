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
            funder_addr = None if is_eoa else getattr(settings, "POLYMARKET_FUNDER_ADDRESS", None)

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

async def get_live_balance() -> float:
    client = get_live_client()
    if not client:
        return 0.0
    try:
        res = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        # The balance is returned in raw USDC atomic units (6 decimals). 
        # But wait, Polymarket returns it as a string representing the exact atomic units?
        # Let's check format. It returned '0'. If 1 USDC = 1000000, we need to divide by 1e6.
        # py_clob_client converts or not? Let's assume we divide by 1e6.
        raw_bal = float(res.get("balance", 0))
        return raw_bal / 1e6
    except Exception as e:
        logger.error(f"Failed to fetch live polymarket balance: {e}")
        return 0.0

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
            return {"ok": False, "error": str(resp.get("errorMsg", "unknown error"))}
    except Exception as e:
        logger.error(f"Failed to place live polymarket order: {e}")
        # if it's PolyApiException, print more details
        if hasattr(e, "status_code"):
            logger.error(f"PolyApiException status={e.status_code} error={getattr(e, 'error_msg', getattr(e, 'error_message', str(e)))}")
        return {"ok": False, "error": str(e)}
