from datetime import datetime, timedelta, timezone

from app.services.intel import _freshness_score, _headline_sentiment, _parse_bls_month, _parse_dt


def test_headline_sentiment_positive_vs_negative():
    positive = _headline_sentiment(["Bitcoin surge as ETF approved"])
    negative = _headline_sentiment(["Stocks crash after inflation rise"])
    assert positive > 0
    assert negative < 0


def test_freshness_score_decay():
    now = datetime.now(timezone.utc)
    fresh = _freshness_score([now - timedelta(minutes=30)])
    stale = _freshness_score([now - timedelta(hours=30)])
    assert fresh > stale
    assert 0.0 <= stale <= 1.0


def test_parse_dt_supports_compact_formats():
    assert _parse_dt("20260503") is not None
    assert _parse_dt("20260503131559") is not None
    assert _parse_dt("2026-05-03T13:15:59Z") is not None


def test_parse_bls_month_validates_period():
    assert _parse_bls_month("2026", "M03") == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert _parse_bls_month("2026", "M13") is None
