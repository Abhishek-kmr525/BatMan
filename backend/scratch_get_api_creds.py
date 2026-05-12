"""
Run this once to get the API credentials for Polymarket.
Tries sig_type 0, 1, 2 with different funders to find what works.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["AMTA_TESTING"] = "1"  # skip DB init

from app.core.config import settings

PRIVATE_KEY = "0xe7ddd4fd0284c3d96e8bdcaffcfd83669f85c87c6302bfe684ab04795d872eb7"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

CONFIGS = [
    (0, None,                                        "EOA no funder"),
    (1, "0xCd7E5Dc7a244B77A3eDe129Fb8AB192b5E700dB3", "Magic funder (confirmed working)"),
    (1, None,                                        "Magic no funder"),
    (2, "0xe2d7e784bb22cd7e8351624382d08901619ccd68", "Gnosis Safe wallet addr"),
]

from py_clob_client_v2.client import ClobClient

for sig_type, funder, label in CONFIGS:
    print(f"\n{'='*60}")
    print(f"Trying: {label} (sig_type={sig_type}, funder={funder})")
    try:
        c = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, 
                       funder=funder, signature_type=sig_type)
        try:
            creds = c.create_api_key()
            method = "create_api_key"
        except Exception as e1:
            print(f"  create_api_key failed: {e1}")
            try:
                creds = c.derive_api_key()
                method = "derive_api_key"
            except Exception as e2:
                print(f"  derive_api_key failed: {e2}")
                continue
        
        c.set_api_creds(creds)
        print(f"  ✅ {method} succeeded!")
        print(f"  POLYMARKET_API_KEY={creds.api_key}")
        print(f"  POLYMARKET_API_SECRET={creds.api_secret}")
        print(f"  POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
        
        # Test balance
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        try:
            res = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            raw_bal = float(res.get("balance", 0))
            balance = raw_bal / 1e6
            print(f"  💰 Balance: ${balance:.4f}")
        except Exception as be:
            print(f"  Balance check failed: {be}")
        break
    except Exception as e:
        print(f"  Client init failed: {e}")
