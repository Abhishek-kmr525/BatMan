"""Tests for the heuristic analyzer fallback."""
from anthropic import AuthenticationError, BadRequestError
from httpx import Request, Response

import pytest

from app.agent.analyzer import (
    _claude_error_reason,
    _heuristic_score,
    _local_skill_score,
    analyze_market,
    check_claude_health,
    check_gemini_health,
)
from app.core.config import settings
from app.services.kalshi import Market


def _market(yes: float, vol: int = 1000, close_s: int = 3600) -> Market:
    return Market("X", "test", "Cat", yes, round(1 - yes, 2), vol, 0, close_s, {})


def test_heuristic_picks_cheaper_side():
    cheap_yes = _heuristic_score(_market(0.12))
    assert cheap_yes["action"] == "BUY_YES"
    cheap_no = _heuristic_score(_market(0.85))
    assert cheap_no["action"] == "BUY_NO"


def test_heuristic_target_above_entry():
    h = _heuristic_score(_market(0.12))
    assert h["target_exit_price"] > h["entry_price"], "target must clear entry"


def test_heuristic_stop_strictly_below_entry():
    """Regression: previous version produced stop >= entry on tiny entries."""
    for yes in [0.04, 0.06, 0.08, 0.12, 0.20, 0.45]:
        h = _heuristic_score(_market(yes))
        assert h["stop_loss_price"] < h["entry_price"], (
            f"stop {h['stop_loss_price']} must be < entry {h['entry_price']} (yes={yes})"
        )


def test_heuristic_realistic_target_size():
    """Targets should be a small move, not the old 0.58-from-anything bug."""
    h = _heuristic_score(_market(0.12))
    move = h["target_exit_price"] - h["entry_price"]
    assert 0.02 <= move <= 0.20, f"target move {move} should be small/realistic"


def test_heuristic_skips_low_score():
    # 50/50 markets with no edge → score below threshold → SKIP
    h = _heuristic_score(_market(0.50))
    assert h["action"] == "SKIP"


def test_heuristic_score_in_bounds():
    for yes in [0.01, 0.20, 0.50, 0.80, 0.99]:
        h = _heuristic_score(_market(yes))
        assert 0 <= h["score"] <= 99


def test_heuristic_tightens_target_near_expiry():
    near_close = _heuristic_score(_market(0.12, vol=1200, close_s=20 * 60))
    later_close = _heuristic_score(_market(0.12, vol=1200, close_s=8 * 3600))
    near_move = near_close["target_exit_price"] - near_close["entry_price"]
    later_move = later_close["target_exit_price"] - later_close["entry_price"]
    assert near_move < later_move


def test_heuristic_skips_very_short_time_even_with_edge():
    h = _heuristic_score(_market(0.12, vol=2000, close_s=6 * 60))
    assert h["action"] == "SKIP"


def test_local_skill_score_skips_tiny_entry_price():
    h = _local_skill_score(_market(0.01, vol=3000, close_s=3 * 3600), [])
    assert h["action"] == "SKIP"


def test_claude_auth_error_is_actionable():
    req = Request("POST", "https://api.anthropic.com/v1/messages")
    resp = Response(
        401,
        request=req,
        json={"error": {"message": "invalid x-api-key"}},
    )
    err = AuthenticationError("invalid x-api-key", response=resp, body=resp.json())
    assert _claude_error_reason(err) == (
        "claude unavailable: authentication failed; replace ANTHROPIC_API_KEY"
    )


def test_claude_bad_request_includes_safe_message():
    req = Request("POST", "https://api.anthropic.com/v1/messages")
    resp = Response(
        400,
        request=req,
        json={"error": {"message": "model: invalid-model is not supported"}},
    )
    err = BadRequestError("bad request", response=resp, body=resp.json())
    assert _claude_error_reason(err) == (
        "claude unavailable: bad request; model: invalid-model is not supported"
    )


def test_local_skill_score_uses_multiple_skills():
    h = _local_skill_score(
        _market(0.12, vol=3000, close_s=3 * 3600),
        [{"text": "Trading probability risk management liquidity breakout", "metadata": {"file_name": "x.pdf", "page": 1}}],
    )
    assert h["provider"] == "local"
    assert set(h["skills"]) == {
        "probability_edge",
        "liquidity",
        "time",
        "market_clarity",
        "knowledge_match",
    }
    assert h["score"] >= 0
    assert h["entry_price"] == 0.12
    assert h["target_exit_price"] > h["entry_price"]
    assert h["stop_loss_price"] < h["entry_price"]


@pytest.mark.asyncio
async def test_analyze_market_local_provider_does_not_call_claude(monkeypatch):
    monkeypatch.setattr(settings, "ANALYZER_PROVIDER", "local")
    monkeypatch.setattr(settings, "LOCAL_ANALYZER_USE_RAG", False)
    rag_called = False

    def fail_if_rag_called(*_args, **_kw):
        nonlocal rag_called
        rag_called = True
        raise AssertionError("RAG should not run when LOCAL_ANALYZER_USE_RAG=false")

    monkeypatch.setattr("app.agent.analyzer.knowledge.query", fail_if_rag_called)
    called = False

    def fail_if_called():
        nonlocal called
        called = True
        raise AssertionError("Claude should not be called in local provider mode")

    monkeypatch.setattr("app.agent.analyzer._claude", fail_if_called)
    analysis = await analyze_market(_market(0.12, vol=3000, close_s=3 * 3600))
    assert not called
    assert not rag_called
    assert analysis.raw["provider"] == "local"
    assert "free local skills" in analysis.reasoning


def test_claude_health_is_non_spending_when_local(monkeypatch):
    monkeypatch.setattr(settings, "ANALYZER_PROVIDER", "local")
    result = check_claude_health()
    assert result["ok"] is False
    assert result["enabled"] is False
    assert "prevents paid API calls" in result["error"]


def test_gemini_health_disabled_does_not_call_api(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_REVIEW_ENABLED", False)
    result = check_gemini_health()
    assert result["ok"] is False
    assert result["enabled"] is False
    assert result["error"] == "gemini review disabled"


def test_gemini_reviewer_can_reject_high_score_signal(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_REVIEW_ENABLED", True)
    monkeypatch.setattr(settings, "GEMINI_REVIEW_MIN_SCORE", 85)
    monkeypatch.setattr("app.agent.analyzer._gemini_quota_available", lambda: True)
    monkeypatch.setattr(
        "app.agent.analyzer._gemini_review",
        lambda *_args, **_kw: {
            "provider": "gemini",
            "status": "reviewed",
            "decision": "REJECT",
            "reason": "event edge is not clear",
        },
    )
    h = _local_skill_score(_market(0.12, vol=3000, close_s=3 * 3600), [])
    from app.agent.analyzer import _maybe_apply_gemini_review
    reviewed = _maybe_apply_gemini_review(_market(0.12, vol=3000, close_s=3 * 3600), h)
    assert reviewed["action"] == "SKIP"
    assert reviewed["ai_review"]["decision"] == "REJECT"
