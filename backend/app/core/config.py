from pathlib import Path
import os

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
# .env wins over shell env for normal app runs, but tests must be able to
# isolate DATABASE_URL/CHROMA_DIR without touching the local app database.
if os.getenv("AMTA_TESTING") != "1":
    for _k, _v in dotenv_values(_ENV_PATH).items():
        if _v:
            os.environ[_k] = _v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None if os.getenv("AMTA_TESTING") == "1" else str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    KALSHI_KEY_ID: str = ""
    KALSHI_PRIVATE_KEY_PATH: str = "./data/kalshi_key.pem"
    KALSHI_PRIVATE_KEY_B64: str = ""
    KALSHI_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"
    KALSHI_DEMO: bool = True
    KALSHI_PAPER_MODE: bool = True

    KNOWLEDGE_PDF_DIR: str = ""
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/amta.db"
    CHROMA_DIR: str = "./data/chroma"

    ANALYZER_PROVIDER: str = "local"  # "local" | "claude"
    LOCAL_ANALYZER_USE_RAG: bool = False
    # When True, skip all scoring and deterministically BUY the favorite side
    # (the higher-priced contract = lower payout = portal's recommendation).
    BYPASS_ANALYZER_BUY_FAVORITE: bool = True
    GEMINI_REVIEW_ENABLED: bool = False
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_REVIEW_MIN_SCORE: int = 85
    GEMINI_DAILY_LIMIT: int = 10
    CLAUDE_MODEL: str = "claude-sonnet-4-5-20250929"
    EMBEDDING_PROVIDER: str = "local"  # "local" | "openai"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"

    INTEL_FEATURES_ENABLED: bool = True
    INTEL_USE_GDELT: bool = True
    INTEL_USE_GUARDIAN: bool = True
    INTEL_USE_FRED: bool = True
    INTEL_USE_BLS: bool = True
    GUARDIAN_API_KEY: str = "test"
    FRED_API_KEY: str = ""
    INTEL_REQUEST_TIMEOUT_SECONDS: float = 12.0
    INTEL_CACHE_TTL_SECONDS: int = 600
    INTEL_GDELT_MIN_INTERVAL_SECONDS: int = 6
    INTEL_MIN_FRESHNESS: float = 0.22
    INTEL_MIN_NEWS_ITEMS: int = 2
    INTEL_STRICT_SKIP: bool = True
    QUICK_EXPIRY_ALWAYS_ON: bool = False
    QUICK_EXPIRY_MIN_SECONDS: int = 60
    QUICK_EXPIRY_MAX_SECONDS: int = 600

    POLYMARKET_PAPER_MODE: bool = True
    POLYMARKET_SCAN_INTERVAL_SECONDS: int = 5
    POLYMARKET_TRADE_AMOUNT_USD: float = 1.00
    POLYMARKET_MAX_OPEN_POSITIONS: int = 40
    POLYMARKET_MIN_SCORE: int = 65
    POLYMARKET_MIN_TIME_TO_CLOSE_SECONDS: int = 60
    POLYMARKET_MAX_TIME_TO_CLOSE_SECONDS: int = 7776000
    POLYMARKET_PREFER_MICRO_UPDOWN: bool = True
    POLYMARKET_MICRO_MAX_TIME_TO_CLOSE_SECONDS: int = 1800
    # Up/down 5m markets often start fresh at $0-$50 volume, so we accept any
    # liquidity by default and lean on AI score instead.
    POLYMARKET_MICRO_MIN_VOLUME: float = 0.0
    POLYMARKET_MAX_OPENS_PER_TICK: int = 12
    # Skip entries where the favorite is weaker than this — markets with the
    # winning side priced below this floor are essentially coin flips and
    # contributed ~67% of full losses in the last 200-trade window.
    POLYMARKET_MIN_FAVORITE_PRICE: float = 0.60
    # 0 = EOA, 1 = POLY_PROXY (Magic email login), 2 = POLY_GNOSIS_SAFE (browser wallet).
    # Browser-wallet accounts (MetaMask etc.) need 2; Magic accounts need 1.
    POLYMARKET_SIGNATURE_TYPE: int = 1
    POLYMARKET_STARTING_BALANCE: float = 20.00
    POLYMARKET_MAX_DAILY_LOSS_USD: float = 6.00

    POLYMARKET_PRIVATE_KEY: str = "0xe7ddd4fd0284c3d96e8bdcaffcfd83669f85c87c6302bfe684ab04795d872eb7"
    POLYMARKET_FUNDER_ADDRESS: str = "0xCd7E5Dc7a244B77A3eDe129Fb8AB192b5E700dB3"
    POLYMARKET_HOST: str = "https://clob.polymarket.com"
    POLYMARKET_CHAIN_ID: int = 137

    KALSHI_MAX_DAILY_LOSS_USD: float = 15.00
    KALSHI_LIVE_ENABLED: bool = False
    POLYMARKET_LIVE_ENABLED: bool = True
    LIVE_MODE_CONFIRM_PASSCODE: str = "9472"
    LIVE_CANARY_ENABLED: bool = True
    KALSHI_CANARY_MAX_ORDER_USD: float = 2.00
    KALSHI_CANARY_MAX_NEW_TRADES_PER_DAY: int = 5
    KALSHI_CANARY_MAX_TOTAL_EXPOSURE_USD: float = 10.00
    POLYMARKET_CANARY_MAX_ORDER_USD: float = 2.00
    POLYMARKET_CANARY_MAX_NEW_TRADES_PER_DAY: int = 5
    POLYMARKET_CANARY_MAX_TOTAL_EXPOSURE_USD: float = 10.00

    BOT_SCAN_INTERVAL_SECONDS: int = 30
    TRADE_AMOUNT_USD: float = 1.00
    MIN_TRADE_SCORE: int = 65
    # Refuse deep-underdog Kalshi tickets — they are negative-EV by default
    # and stop-loss whipsaw eats the few wins. 0.20 keeps us in markets where
    # a few-cent move is feasible and stops are wider in absolute terms.
    MIN_ENTRY_PRICE: float = 0.20
    MAX_ENTRY_PRICE: float = 0.85
    # Skip entering a sister bucket if we already hold a position in the same
    # series (e.g. multiple "Bitcoin price on May 3" strikes).
    DEDUP_BUCKET_SERIES: bool = True
    MAX_CONCURRENT_POSITIONS: int = 20
    MAX_ANALYSES_PER_TICK: int = 20
    STARTING_BALANCE: float = 10000.00

    API_PORT: int = 4000
    MAINTENANCE_PASSCODE: str = "9472"


settings = Settings()


def normalize_db_url(url: str) -> str:
    if url.startswith("sqlite:///") and "+aiosqlite" not in url:
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url
