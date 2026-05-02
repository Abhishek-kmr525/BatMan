"""Strategy filter + activation tests."""
import pytest

from app.services import strategies
from app.services.kalshi import Market


def _m(ticker: str, yes: float, vol: int, close_s: int) -> Market:
    return Market(ticker, ticker, "X", yes, round(1 - yes, 2), vol, 0, close_s, {})


def test_activate_round_trip():
    strategies.activate("longshot_value")
    assert strategies.is_active("longshot_value")
    assert [s.id for s in strategies.get_active_list()] == ["longshot_value"]
    strategies.deactivate("longshot_value")
    assert not strategies.is_active("longshot_value")
    assert strategies.get_active_list() == []


def test_activate_unknown_raises():
    with pytest.raises(ValueError):
        strategies.activate("does_not_exist")
    with pytest.raises(ValueError):
        strategies.deactivate("does_not_exist")


def test_multiple_active_preserves_order():
    strategies.activate("longshot_value")
    strategies.activate("sports_props")
    ids = [s.id for s in strategies.get_active_list()]
    assert ids == ["longshot_value", "sports_props"]


def test_claim_returns_first_matching_active_strategy():
    """First-active-wins when multiple strategies could claim a market."""
    strategies.activate("sports_props")
    strategies.activate("balanced_default")
    # MLB market matches BOTH sports_props (keyword) and balanced_default (volume)
    m = _m("KXMLBHIT-x", 0.20, 200, 3600)
    claimed = strategies.claim(m)
    assert claimed.id == "sports_props"  # activated first


def test_claim_no_active_returns_none():
    m = _m("KXWTI-x", 0.20, 200, 3600)
    assert strategies.claim(m) is None


def test_claim_filter_mismatch_falls_to_next():
    strategies.activate("longshot_value")  # needs yes <= 0.20
    strategies.activate("balanced_default")  # any volume >= 10
    m = _m("X", 0.50, 200, 3600)  # too expensive for longshot
    claimed = strategies.claim(m)
    assert claimed.id == "balanced_default"


def test_longshot_filter():
    s = strategies.get_strategy("longshot_value")
    assert s.filter(_m("a", 0.10, 100, 3600))   # cheap + liquid → match
    assert not s.filter(_m("b", 0.10, 5, 3600))  # too thin
    assert not s.filter(_m("c", 0.50, 500, 3600))  # too expensive


def test_near_expiry_filter():
    s = strategies.get_strategy("near_expiry_momentum")
    assert s.filter(_m("a", 0.30, 200, 60 * 60))     # 1h, fine
    assert not s.filter(_m("b", 0.30, 200, 6 * 3600))  # 6h, too far
    assert not s.filter(_m("c", 0.30, 50, 60 * 60))   # too thin


def test_high_volume_filter():
    s = strategies.get_strategy("high_volume_mainstream")
    assert s.filter(_m("a", 0.30, 600, 3600))
    assert not s.filter(_m("b", 0.30, 100, 3600))


def test_sports_props_filter_matches_known_prefixes():
    s = strategies.get_strategy("sports_props")
    assert s.filter(_m("KXMLBHIT-x", 0.20, 100, 3600))
    assert s.filter(_m("KXNBAGAME-y", 0.45, 100, 3600))
    assert s.filter(_m("KXATPMATCH-z", 0.30, 100, 3600))
    assert not s.filter(_m("KXWTI-q", 0.30, 100, 3600))


def test_balanced_default_filter_is_permissive():
    s = strategies.get_strategy("balanced_default")
    assert s.filter(_m("a", 0.30, 50, 3600))
    assert not s.filter(_m("b", 0.30, 5, 3600))  # min volume 10
