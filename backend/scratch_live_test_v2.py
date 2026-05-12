import asyncio
import logging
from app.core.config import settings
from app.services.poly_live import place_live_order, get_live_balance

logging.basicConfig(level=logging.INFO)

async def test_order(sig_type):
    settings.POLYMARKET_SIGNATURE_TYPE = sig_type
    settings.POLYMARKET_FUNDER_ADDRESS = None
    
    import app.services.poly_live
    app.services.poly_live._client = None
    app.services.poly_live.is_eoa = True
    
    from app.services.polymarket import get_polymarket
    poly = get_polymarket()
    markets = await poly.get_markets(limit=100)
    target_market = None
    for m in markets:
        if m.volume > 0:
            target_market = m
            break
            
    if target_market:
        print(f"Testing with sig_type={sig_type}")
        raw = target_market.raw
        import json
        tokens = raw.get('clobTokenIds')
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        token_id = tokens[0]
        price = 0.01
        size = 1.0 # 1 contract
        res = await place_live_order(str(token_id), price, size, "BUY")
        print(f"Result: {res}")
    else:
        print("No active market found")

async def main():
    await test_order(2)

if __name__ == "__main__":
    asyncio.run(main())
