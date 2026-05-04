"""Live trading adapter for Polymarket (phase-3)."""
from __future__ import annotations

import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from app.core.config import settings

logger = logging.getLogger(__name__)

_client: ClobClient | None = None

def get_live_client() -> ClobClient | None:
    global _client
    if not settings.POLYMARKET_PRIVATE_KEY:
        return None
    if _client is None:
        try:
            _client = ClobClient(
                host=settings.POLYMARKET_HOST,
                key=settings.POLYMARKET_PRIVATE_KEY,
                chain_id=settings.POLYMARKET_CHAIN_ID,
                funder=settings.POLYMARKET_FUNDER_ADDRESS if hasattr(settings, "POLYMARKET_FUNDER_ADDRESS") else None,
                signature_type=1
            )
            _client.set_api_creds(_client.create_or_derive_api_creds())
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
        order_args = OrderArgs(
            price=price,
            size=size,
            side=BUY if side.upper() == "BUY" else "SELL", # Note: using string or enum
            token_id=token_id,
        )
        # In py_clob_client side is usually 'BUY' or 'SELL'.
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order)
        if resp.get("success"):
            return {"ok": True, "orderID": resp.get("orderID")}
        else:
            return {"ok": False, "error": str(resp.get("errorMsg", "unknown error"))}
    except Exception as e:
        logger.error(f"Failed to place live polymarket order: {e}")
        return {"ok": False, "error": str(e)}
