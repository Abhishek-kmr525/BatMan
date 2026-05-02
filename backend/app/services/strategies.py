"""Strategy Marketplace.

A strategy is a named bundle of (filter, threshold) overrides applied to
the bot's market-selection step. Filters narrow the candidate pool;
threshold overrides the global MIN_TRADE_SCORE for trades opened while
the strategy is active.

Trades opened while a strategy is active are tagged with strategy_id
so we can compute per-strategy performance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.services.kalshi import Market


@dataclass
class Strategy:
    id: str
    name: str
    description: str
    min_score: int
    filter: Callable[[Market], bool]
    sort_key: Callable[[Market], float]  # higher = preferred


def _has_keywords(m: Market, words: list[str]) -> bool:
    t = (m.ticker + " " + (m.title or "")).lower()
    return any(w in t for w in words)


STRATEGIES: list[Strategy] = [
    Strategy(
        id="longshot_value",
        name="Long-shot value bets",
        description=(
            "Cheap YES contracts (<= $0.20) with non-trivial volume. "
            "Asymmetric upside — small loss, big payoff if it hits."
        ),
        min_score=70,
        filter=lambda m: m.yes_price <= 0.20 and m.volume >= 50,
        sort_key=lambda m: m.volume,
    ),
    Strategy(
        id="near_expiry_momentum",
        name="Near-expiry momentum",
        description=(
            "Markets closing within 4 hours with strong volume. "
            "Time pressure typically resolves into a clean YES/NO."
        ),
        min_score=65,
        filter=lambda m: m.close_time_seconds <= 4 * 3600 and m.volume >= 100,
        sort_key=lambda m: m.volume,
    ),
    Strategy(
        id="high_volume_mainstream",
        name="High-volume mainstream",
        description=(
            "Only markets with deep liquidity (volume >= 500). "
            "Tighter spreads, easier exits."
        ),
        min_score=65,
        filter=lambda m: m.volume >= 500,
        sort_key=lambda m: m.volume,
    ),
    Strategy(
        id="sports_props",
        name="Sports player props",
        description=(
            "MLB/NBA/NFL/tennis player and game props. "
            "Typically resolve same day with clear data."
        ),
        min_score=65,
        filter=lambda m: _has_keywords(
            m, ["mlb", "nba", "nfl", "atp", "wta", "kxhit", "kxks", "kxgame"]
        ),
        sort_key=lambda m: m.volume,
    ),
    Strategy(
        id="balanced_default",
        name="Balanced (default)",
        description=(
            "Default behavior: any liquid market with > 5 minutes to close. "
            "Equivalent to no strategy active."
        ),
        min_score=65,
        filter=lambda m: m.volume >= 10,
        sort_key=lambda m: m.volume,
    ),
]


def get_strategy(strategy_id: str) -> Strategy | None:
    for s in STRATEGIES:
        if s.id == strategy_id:
            return s
    return None


def public_view(s: Strategy) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "min_score": s.min_score,
    }


# In-memory active strategies (single-user MVP).
# Ordered list — earlier ids win when multiple strategies match the same
# market (first-active-wins).
_active_ids: list[str] = []


def get_active_list() -> list[Strategy]:
    out: list[Strategy] = []
    for sid in _active_ids:
        s = get_strategy(sid)
        if s is not None:
            out.append(s)
    return out


def is_active(strategy_id: str) -> bool:
    return strategy_id in _active_ids


def activate(strategy_id: str) -> Strategy:
    s = get_strategy(strategy_id)
    if s is None:
        raise ValueError(f"unknown strategy: {strategy_id}")
    if s.id not in _active_ids:
        _active_ids.append(s.id)
    return s


def deactivate(strategy_id: str) -> Strategy | None:
    s = get_strategy(strategy_id)
    if s is None:
        raise ValueError(f"unknown strategy: {strategy_id}")
    if s.id in _active_ids:
        _active_ids.remove(s.id)
    return s


def deactivate_all() -> None:
    _active_ids.clear()


def claim(market: Market) -> Strategy | None:
    """Return the first active strategy whose filter matches this market."""
    for s in get_active_list():
        if s.filter(market):
            return s
    return None
