"""Pure-function tests for the Kalshi parser. No network."""
from app.services.kalshi import (
    _category_from_ticker,
    _is_parlay,
    _parse_market,
)


def test_category_from_ticker_known_prefixes():
    assert _category_from_ticker("KXWTI-26APR29-T106.99") == "Oil"
    assert _category_from_ticker("KXBRENT-26") == "Oil"
    assert _category_from_ticker("KXMLBHIT-foo") == "MLB"
    assert _category_from_ticker("KXNBAGAME-bar") == "NBA"
    assert _category_from_ticker("KXATPCHALLENGER-x") == "Tennis"
    assert _category_from_ticker("KXFEDDECISION-25") == "Macro"
    assert _category_from_ticker("KXCPI-26") == "Macro"
    assert _category_from_ticker("KXBTCD-25") == "Crypto"
    assert _category_from_ticker("KXPRES-2028") == "Politics"


def test_category_from_ticker_unknown_falls_through():
    assert _category_from_ticker("ZZZ123") == "Other"
    assert _category_from_ticker("") == ""


def test_is_parlay_detects_mvecollection():
    assert _is_parlay({"mve_collection_ticker": "KXMVECROSS-R"}) is True
    assert _is_parlay({"ticker": "KXMVECROSSCATEGORY-S2026"}) is True
    assert _is_parlay({"ticker": "KXMVESPORTS-X"}) is True
    assert _is_parlay({"ticker": "KXMV-anything"}) is True
    assert _is_parlay({"ticker": "KXWTI-26APR29-T106"}) is False


def test_parse_market_uses_dollar_fields():
    raw = {
        "ticker": "KXWTI-26APR29-T106.99",
        "title": "Oil > 106.99?",
        "yes_bid_dollars": 0.10,
        "yes_ask_dollars": 0.14,
        "volume_24h_fp": 636.0,
        "open_interest_fp": 200.0,
        "close_time": "2099-01-01T00:00:00Z",
    }
    m = _parse_market(raw)
    assert m.ticker == "KXWTI-26APR29-T106.99"
    assert m.yes_price == 0.12  # midpoint
    assert m.no_price == 0.88
    assert m.volume == 636
    assert m.open_interest == 200
    assert m.category == "Oil"  # derived since raw['category'] missing


def test_parse_market_falls_back_to_cents():
    raw = {
        "ticker": "OLD-FORMAT",
        "yes_bid": 12,  # cents
        "yes_ask": 18,
        "volume": 500,
        "category": "Politics",
    }
    m = _parse_market(raw)
    assert m.yes_price == 0.15
    assert m.volume == 500
    assert m.category == "Politics"  # respect explicit category over derived


def test_parse_market_close_time_relative():
    raw = {"ticker": "X", "close_time": "1970-01-01T00:00:00Z"}
    m = _parse_market(raw)
    assert m.close_time_seconds == 0  # past dates floor at 0
