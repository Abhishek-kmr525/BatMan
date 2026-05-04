"""Analysis Engine — free local multi-skill scoring with optional Claude."""
from __future__ import annotations

import json
from datetime import date
from dataclasses import dataclass

import httpx
from anthropic import (
    APIConnectionError,
    APIStatusError,
    Anthropic,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from app.agent import knowledge
from app.core.config import settings
from app.services.intel import gather_market_intel
from app.services.kalshi import Market

_client: Anthropic | None = None
_gemini_usage_day: str | None = None
_gemini_usage_count = 0


def _claude() -> Anthropic:
    global _client
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("missing ANTHROPIC_API_KEY")
    if _client is None:
        _client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


SYSTEM_PROMPT = """You are AMTA, an AI Master Trading Agent for Kalshi event contracts.
You analyze a single market using expert trading knowledge from the provided context.
You always respond in strict JSON only — no prose, no markdown.

Trade rules:
- Position size is fixed at $1.00 per trade. Do not propose larger sizes.
- Only recommend a trade if score >= 65/100.
- Score weights: PDF strategy match 35, probability edge 25, time value 20, liquidity 10, clarity 10.
- target_exit_price and stop_loss_price are in 0..1 (probability), on the same side as your action.
- action must be one of: BUY_YES, BUY_NO, SKIP.

Output schema:
{
  "score": int 0-100,
  "action": "BUY_YES" | "BUY_NO" | "SKIP",
  "confidence": float 0..1,
  "entry_price": float 0..1,
  "target_exit_price": float 0..1,
  "stop_loss_price": float 0..1,
  "reasoning": str (<=240 chars),
  "knowledge_sources": [str]
}
"""


@dataclass
class Analysis:
    score: int
    action: str
    confidence: float
    entry_price: float
    target_exit_price: float
    stop_loss_price: float
    reasoning: str
    knowledge_sources: list[str]
    raw: dict


def _build_user_prompt(market: Market, chunks: list[dict]) -> str:
    ctx_blocks = []
    for c in chunks:
        meta = c.get("metadata") or {}
        src = f"{meta.get('file_name','?')} p.{meta.get('page','?')}"
        ctx_blocks.append(f"[{src}] {c['text'][:800]}")
    ctx = "\n\n".join(ctx_blocks) if ctx_blocks else "(no PDF context available)"
    market_json = json.dumps(
        {
            "ticker": market.ticker,
            "title": market.title,
            "category": market.category,
            "yes_price": market.yes_price,
            "no_price": market.no_price,
            "volume": market.volume,
            "open_interest": market.open_interest,
            "time_to_close_seconds": market.close_time_seconds,
        },
        indent=2,
    )
    return (
        f"KNOWLEDGE BASE CONTEXT:\n{ctx}\n\n"
        f"MARKET:\n{market_json}\n\n"
        "Return JSON only."
    )


def _build_user_prompt_with_intel(market: Market, chunks: list[dict], intel: dict) -> str:
    base = _build_user_prompt(market, chunks)
    intel_json = json.dumps(intel, indent=2)
    return f"{base}\n\nEXTERNAL_INTEL:\n{intel_json}\n\nReturn JSON only."


def _favorite_side_analysis(market: Market) -> Analysis:
    """Deterministic: buy the favorite (higher-priced side = lower payout)."""
    yes = market.yes_price
    no = market.no_price
    side = "YES" if yes >= no else "NO"
    entry = yes if side == "YES" else no
    target = round(min(0.99, entry + 0.05), 2)
    stop = round(max(0.01, entry - 0.20), 2)
    return Analysis(
        score=99,
        action=f"BUY_{side}",
        confidence=0.99,
        entry_price=entry,
        target_exit_price=target,
        stop_loss_price=stop,
        reasoning=f"favorite-mode: BUY {side} @ {entry:.2f} (yes={yes:.2f}, no={no:.2f})",
        knowledge_sources=[],
        raw={"mode": "favorite", "yes": yes, "no": no},
    )


async def analyze_market(market: Market) -> Analysis:
    if settings.BYPASS_ANALYZER_BUY_FAVORITE:
        return _favorite_side_analysis(market)

    rag_query = f"Kalshi {market.category} market: {market.title}"
    intel = await gather_market_intel(market)
    intel_dict = intel.as_dict()

    if settings.ANALYZER_PROVIDER.lower() != "claude":
        chunks = _safe_local_context(rag_query) if settings.LOCAL_ANALYZER_USE_RAG else []
        data = _local_skill_score(market, chunks, intel_dict)
        data = _maybe_apply_gemini_review(market, data)
        data["intel"] = intel_dict
        sources = data.get("knowledge_sources") or [
            f"{(c['metadata'] or {}).get('file_name','?')} p.{(c['metadata'] or {}).get('page','?')}"
            for c in chunks
        ]
        return _analysis_from_data(market, data, sources)

    chunks = knowledge.query(rag_query, k=5)
    try:
        msg = _claude().messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt_with_intel(market, chunks, intel_dict)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        text = _strip_code_fence(text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = _heuristic_score(market, reason="claude returned non-JSON")
    except Exception as e:
        # API failure (credits, network, rate limits) — fall back to heuristic
        # so the bot can demo end-to-end. Real Claude scoring resumes automatically.
        data = _heuristic_score(market, reason=_claude_error_reason(e))
    sources = data.get("knowledge_sources") or [
        f"{(c['metadata'] or {}).get('file_name','?')} p.{(c['metadata'] or {}).get('page','?')}"
        for c in chunks
    ]
    data["intel"] = intel_dict
    return _analysis_from_data(market, data, sources)


def _analysis_from_data(market: Market, data: dict, sources: list[str]) -> Analysis:
    return Analysis(
        score=int(data.get("score", 0)),
        action=str(data.get("action", "SKIP")).upper(),
        confidence=float(data.get("confidence", 0.0)),
        entry_price=float(data.get("entry_price", market.yes_price)),
        target_exit_price=float(data.get("target_exit_price", market.yes_price)),
        stop_loss_price=float(data.get("stop_loss_price", market.yes_price)),
        reasoning=str(data.get("reasoning", ""))[:300],
        knowledge_sources=list(sources),
        raw=data,
    )


def _local_skill_score(market: Market, chunks: list[dict], intel: dict | None = None) -> dict:
    """Free analyzer assembled from small deterministic trading skills."""
    side_signal = _skill_side_and_edge(market)
    liquidity = _skill_liquidity(market)
    time_signal = _skill_time(market)
    clarity = _skill_clarity(market)
    knowledge_signal = _skill_knowledge_match(market, chunks)
    intel_signal = _skill_external_intel(intel)

    entry = side_signal["entry"]

    # --- Sports-aware gating: require >10pp model vs market deviation ---
    sports_deviation_ok = True
    if getattr(market, "category", None) == "Sports":
        # Model's probability: use side_signal["entry"] as the bot's implied probability for the chosen side
        # Market's probability: use yes_price or no_price depending on side
        market_prob = market.yes_price if side_signal["side"] == "YES" else market.no_price
        model_prob = side_signal["entry"]
        deviation = abs(model_prob - market_prob)
        sports_deviation_ok = deviation >= 0.10

    # --- Re-weight scoring function ---
    score_raw = (
        18
        + side_signal["score"] * 0.10  # was 0.34, now 0.10
        + liquidity["score"] * 0.14     # was 0.18, now 0.14
        + time_signal["score"] * 0.14   # was 0.16, now 0.14
        + clarity["score"] * 0.18       # was 0.12, now 0.18
        + knowledge_signal["score"] * 0.26 # was 0.18, now 0.26
        + intel_signal["score"] * 0.18     # was 0.08, now 0.18
    )
    score = int(round(_clamp(score_raw, 0.0, 99.0)))

    target, stop, tp_move, sl_move = _risk_prices(
        entry=entry,
        edge_norm=side_signal["edge_norm"],
        liquidity_norm=liquidity["norm"],
        time_quality=time_signal["quality"],
        close_seconds=market.close_time_seconds,
    )

    confidence = round(
        _clamp(
            0.12
            + side_signal["edge_norm"] * 0.36
            + liquidity["norm"] * 0.18
            + time_signal["quality"] * 0.18
            + clarity["norm"] * 0.08
            + knowledge_signal["norm"] * 0.08
            + intel_signal["norm"] * 0.06,
            0.05,
            0.95,
        ),
        2,
    )
    confidence = round(_clamp(confidence * intel_signal["confidence_multiplier"], 0.05, 0.95), 2)

    # Allow deep-underdog tickets only when knowledge or external intel
    # gives us a concrete contrarian thesis (knowledge_norm or intel_norm
    # >= 0.6) — otherwise we're just buying the long tail.
    has_contrarian_thesis = (
        knowledge_signal["norm"] >= 0.6 or intel_signal["norm"] >= 0.6
    )
    price_floor = settings.MIN_ENTRY_PRICE if not has_contrarian_thesis else 0.05
    price_ceiling = settings.MAX_ENTRY_PRICE

    tradable = (
        score >= settings.MIN_TRADE_SCORE
        and side_signal["edge_norm"] >= 0.45
        and entry >= price_floor
        and entry <= price_ceiling
        and market.volume >= 25
        and market.close_time_seconds >= 8 * 60
        and clarity["norm"] >= 0.35
        and not intel_signal["block_trade"]
        and sports_deviation_ok
    )

    reason_parts = [
        f"free local skills",
        f"{side_signal['side']} @ {entry:.2f}",
        f"edge {side_signal['edge']:.2f}",
        f"liq {liquidity['norm']:.2f}",
        f"time {time_signal['label']}",
        f"kb {knowledge_signal['label']}",
        f"intel {intel_signal['label']}",
        f"target {target:.2f} (+{tp_move:.2f})",
        f"stop {stop:.2f} (-{sl_move:.2f})",
    ]
    if getattr(market, "category", None) == "Sports":
        reason_parts.append(f"sports_deviation_ok={sports_deviation_ok}")

    return {
        "score": score,
        "action": f"BUY_{side_signal['side']}" if tradable else "SKIP",
        "confidence": confidence,
        "entry_price": entry,
        "target_exit_price": target,
        "stop_loss_price": stop,
        "reasoning": "; ".join(reason_parts),
        "knowledge_sources": knowledge_signal["sources"],
        "skills": {
            "probability_edge": side_signal,
            "liquidity": liquidity,
            "time": time_signal,
            "market_clarity": clarity,
            "knowledge_match": knowledge_signal,
            "external_intel": intel_signal,
        },
        "provider": "local",
    }


def _maybe_apply_gemini_review(market: Market, data: dict) -> dict:
    if not settings.GEMINI_REVIEW_ENABLED:
        data["ai_review"] = {"provider": "gemini", "status": "disabled"}
        return data
    if int(data.get("score", 0)) < settings.GEMINI_REVIEW_MIN_SCORE:
        data["ai_review"] = {
            "provider": "gemini",
            "status": "skipped_low_score",
            "min_score": settings.GEMINI_REVIEW_MIN_SCORE,
        }
        return data
    if not _gemini_quota_available():
        data["ai_review"] = {
            "provider": "gemini",
            "status": "skipped_daily_limit",
            "daily_limit": settings.GEMINI_DAILY_LIMIT,
        }
        return data
    review = _gemini_review(market, data)
    data["ai_review"] = review
    if review.get("decision") == "REJECT":
        data["action"] = "SKIP"
        data["reasoning"] = f"{data['reasoning']}; gemini rejected: {review.get('reason', '')}"[:300]
    elif review.get("decision") == "CAUTION":
        data["confidence"] = round(max(0.05, float(data.get("confidence", 0.0)) - 0.08), 2)
        data["reasoning"] = f"{data['reasoning']}; gemini caution: {review.get('reason', '')}"[:300]
    elif review.get("decision") == "APPROVE":
        data["reasoning"] = f"{data['reasoning']}; gemini approved"[:300]
    return data


def _gemini_quota_available() -> bool:
    global _gemini_usage_day, _gemini_usage_count
    today = date.today().isoformat()
    if _gemini_usage_day != today:
        _gemini_usage_day = today
        _gemini_usage_count = 0
    return _gemini_usage_count < settings.GEMINI_DAILY_LIMIT


def _gemini_mark_used() -> None:
    global _gemini_usage_count
    _gemini_usage_count += 1


def _gemini_review(market: Market, data: dict) -> dict:
    if not settings.GEMINI_API_KEY:
        return {"provider": "gemini", "status": "missing_key"}
    prompt = {
        "task": (
            "Review this Kalshi trade signal. Return only a compact JSON object "
            "with decision and reason. No preamble."
        ),
        "allowed_schema": {
            "decision": "APPROVE | CAUTION | REJECT",
            "reason": "short reason under 120 chars",
        },
        "rules": [
            "Do not create a new trade idea.",
            "Reject if the trade relies only on cheap price without a clear event edge.",
            "Use CAUTION for correlated, unclear, long-dated, or thin markets.",
            "APPROVE only if the local signal is reasonable for paper-trading review.",
        ],
        "market": {
            "ticker": market.ticker,
            "title": market.title,
            "category": market.category,
            "yes_price": market.yes_price,
            "no_price": market.no_price,
            "volume": market.volume,
            "open_interest": market.open_interest,
            "time_to_close_seconds": market.close_time_seconds,
        },
        "local_signal": {
            "score": data.get("score"),
            "action": data.get("action"),
            "confidence": data.get("confidence"),
            "entry_price": data.get("entry_price"),
            "target_exit_price": data.get("target_exit_price"),
            "stop_loss_price": data.get("stop_loss_price"),
            "reasoning": data.get("reasoning"),
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.GEMINI_MODEL}:generateContent"
    )
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                url,
                params={"key": settings.GEMINI_API_KEY},
                json={
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps(prompt)}],
                        }
                    ],
                    "generationConfig": {
                        "temperature": 0.1,
                        "maxOutputTokens": 512,
                        "responseMimeType": "application/json",
                        "thinkingConfig": {"thinkingBudget": 0},
                    },
                },
            )
        _gemini_mark_used()
        if resp.status_code >= 400:
            return {
                "provider": "gemini",
                "status": "error",
                "error": _gemini_error_message(resp),
            }
        payload = resp.json()
        text = _gemini_text(payload)
        parsed = _parse_review_json(text)
        decision = str(parsed.get("decision", "CAUTION")).upper()
        if decision not in {"APPROVE", "CAUTION", "REJECT"}:
            decision = "CAUTION"
        return {
            "provider": "gemini",
            "status": "reviewed",
            "model": settings.GEMINI_MODEL,
            "decision": decision,
            "reason": str(parsed.get("reason", ""))[:160],
        }
    except Exception as exc:
        return {
            "provider": "gemini",
            "status": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def _parse_review_json(text: str) -> dict:
    cleaned = _strip_code_fence(text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def _gemini_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return "{}"
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    return "".join(str(p.get("text", "")) for p in parts).strip() or "{}"


def _gemini_error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") or {}
        return str(err.get("message") or body)[:180]
    except Exception:
        return resp.text[:180]


def check_gemini_health() -> dict:
    if not settings.GEMINI_REVIEW_ENABLED:
        return {
            "ok": False,
            "enabled": False,
            "provider": "gemini",
            "model": settings.GEMINI_MODEL,
            "error": "gemini review disabled",
        }
    if not settings.GEMINI_API_KEY:
        return {
            "ok": False,
            "enabled": True,
            "provider": "gemini",
            "model": settings.GEMINI_MODEL,
            "error": "missing GEMINI_API_KEY",
        }
    review = _gemini_review(
        Market("HEALTH", "Will this health check return OK?", "System", 0.5, 0.5, 100, 100, 3600, {}),
        {"score": 99, "action": "SKIP", "confidence": 0.0, "reasoning": "health check"},
    )
    return {
        "ok": review.get("status") == "reviewed",
        "enabled": True,
        "provider": "gemini",
        "model": settings.GEMINI_MODEL,
        "daily_limit": settings.GEMINI_DAILY_LIMIT,
        "used_today": _gemini_usage_count,
        "review": review,
    }


def _safe_local_context(query: str) -> list[dict]:
    """Optional RAG context for local mode; never let retrieval block trading."""
    try:
        return knowledge.query(query, k=5)
    except Exception:
        return []


def _heuristic_score(market: Market, reason: str = "heuristic") -> dict:
    """Fallback model when Claude is unavailable.

    Uses market structure signals (edge, liquidity, time-to-close) to:
    - choose side (cheaper contract),
    - compute dynamic take-profit / stop-loss distances,
    - gate weak setups to SKIP instead of forcing low-quality entries.
    """
    yes = market.yes_price
    side = "YES" if yes <= 0.5 else "NO"
    entry = yes if side == "YES" else market.no_price
    edge = max(0.0, 0.5 - entry)  # 0..0.5; larger means cheaper, more asymmetric
    edge_norm = _clamp(edge / 0.35, 0.0, 1.0)

    vol_norm = _clamp(market.volume / 5_000.0, 0.0, 1.0)
    oi_norm = _clamp(market.open_interest / 2_000.0, 0.0, 1.0)
    liquidity = 0.75 * vol_norm + 0.25 * oi_norm
    time_quality = _time_quality(market.close_time_seconds)

    # Penalize markets near 50/50 where the fallback has little true edge.
    mid_penalty = _clamp((0.10 - edge) / 0.10, 0.0, 1.0)

    score_raw = 44 + edge_norm * 32 + liquidity * 14 + time_quality * 10 - mid_penalty * 18
    score = int(round(_clamp(score_raw, 0.0, 99.0)))

    # Dynamic exits: wider when structure is stronger / more time available,
    # tighter when close to expiry.
    base_move = 0.02 + edge_norm * 0.07 + liquidity * 0.04
    tp_mult = _tp_time_multiplier(market.close_time_seconds)
    tp_move = round(_clamp(base_move * tp_mult, 0.02, 0.20), 2)

    # Maintain asymmetric reward/risk while capping for tiny entries.
    rr_to_stop = 0.58 + (1.0 - liquidity) * 0.15 + (1.0 - time_quality) * 0.12
    stop_cap = max(0.01, min(0.15, round(entry - 0.01, 2)))
    sl_move = round(_clamp(tp_move * rr_to_stop, 0.01, stop_cap), 2)

    target = round(_clamp(entry + tp_move, 0.01, 0.97), 2)
    stop = round(_clamp(entry - sl_move, 0.01, 0.96), 2)
    if stop >= entry:
        stop = round(max(0.01, entry - 0.01), 2)

    confidence_raw = (
        0.20 + edge_norm * 0.45 + liquidity * 0.20 + time_quality * 0.15 - mid_penalty * 0.20
    )
    confidence = round(_clamp(confidence_raw, 0.05, 0.95), 2)

    tradable = (
        score >= 65
        and edge >= 0.06
        and entry >= settings.MIN_ENTRY_PRICE
        and market.volume >= 25
        and market.close_time_seconds >= 8 * 60
    )

    return {
        "score": score,
        "action": f"BUY_{side}" if tradable else "SKIP",
        "confidence": confidence,
        "entry_price": entry,
        "target_exit_price": target,
        "stop_loss_price": stop,
        "reasoning": (
            f"{reason}; {side} @ {entry:.2f}, "
            f"score={score}, liq={liquidity:.2f}, t={market.close_time_seconds}s, "
            f"target {target:.2f} (+{tp_move:.2f}), stop {stop:.2f} (-{sl_move:.2f})"
        ),
        "knowledge_sources": [],
    }


def _skill_side_and_edge(market: Market) -> dict:
    """Price-quality skill.

    On Kalshi the displayed price IS the consensus probability. Buying the
    cheap side blindly is a negative-EV bet on the long tail. This skill now
    rewards entries in the uncertain "Goldilocks" zone (~$0.30-$0.70) and
    penalises both deep-underdog tickets (<$0.20) and expensive favourites
    (>$0.80). The bot still picks the cheaper side by default, but the score
    no longer encourages buying $0.03 lottery tickets.
    """
    yes = market.yes_price
    no = market.no_price
    side = "YES" if yes <= no else "NO"
    entry = yes if side == "YES" else no

    # Goldilocks centred at 0.50, peaking when price is between 0.30 and 0.70.
    # Hard floor: anything <0.20 gets a near-zero quality score.
    if entry < 0.10:
        quality = 0.05
    elif entry < 0.20:
        quality = 0.20
    elif entry < 0.30:
        quality = 0.55
    elif entry <= 0.70:
        quality = 1.0
    elif entry <= 0.85:
        quality = 0.55
    else:
        quality = 0.25

    # Distance from 0.50 still matters a little — symmetric uncertainty bonus.
    uncertainty = 1.0 - min(0.5, abs(entry - 0.5)) * 2.0  # 1.0 at 0.50, 0.0 at 0/1
    edge_norm = _clamp(0.7 * quality + 0.3 * uncertainty, 0.0, 1.0)

    # Keep the legacy "edge" key for downstream code that reads it, but make
    # it reflect price quality rather than mere cheapness.
    edge = round(edge_norm * 0.35, 4)

    return {
        "side": side,
        "entry": entry,
        "edge": edge,
        "edge_norm": edge_norm,
        "quality": quality,
        "score": _clamp(edge_norm * 100, 0.0, 100.0),
    }


def _skill_liquidity(market: Market) -> dict:
    vol_norm = _clamp(market.volume / 5_000.0, 0.0, 1.0)
    oi_norm = _clamp(market.open_interest / 2_000.0, 0.0, 1.0)
    norm = 0.75 * vol_norm + 0.25 * oi_norm
    return {"norm": norm, "score": norm * 100, "volume": market.volume}


def _skill_time(market: Market) -> dict:
    quality = _time_quality(market.close_time_seconds)
    if market.close_time_seconds < 15 * 60:
        label = "too close"
    elif market.close_time_seconds < 60 * 60:
        label = "near expiry"
    elif market.close_time_seconds < 6 * 60 * 60:
        label = "active window"
    elif market.close_time_seconds < 24 * 60 * 60:
        label = "same day"
    else:
        label = "longer dated"
    return {"quality": quality, "score": quality * 100, "label": label}


def _skill_clarity(market: Market) -> dict:
    title = (market.title or "").strip()
    has_question = "?" in title or title.lower().startswith(("will ", "who ", "what "))
    has_binary_words = any(w in title.lower() for w in ("above", "below", "win", "close", "reach"))
    length_score = 1.0 if 12 <= len(title) <= 180 else 0.55
    norm = _clamp((0.45 if has_question else 0.2) + (0.35 if has_binary_words else 0.1) + length_score * 0.2, 0.0, 1.0)
    return {"norm": norm, "score": norm * 100}


def _skill_knowledge_match(market: Market, chunks: list[dict]) -> dict:
    if not chunks:
        return {"norm": 0.2, "score": 20.0, "label": "no pdf match", "sources": []}

    market_terms = _terms(f"{market.category} {market.title}")
    best = 0.0
    sources: list[str] = []
    for chunk in chunks[:5]:
        text = str(chunk.get("text") or "")
        overlap = len(market_terms & _terms(text))
        score = _clamp(overlap / 8.0, 0.0, 1.0)
        if score > best:
            best = score
        meta = chunk.get("metadata") or {}
        src = f"{meta.get('file_name','?')} p.{meta.get('page','?')}"
        if src not in sources:
            sources.append(src)

    if best >= 0.7:
        label = "strong pdf match"
    elif best >= 0.35:
        label = "partial pdf match"
    else:
        label = "weak pdf match"
    return {"norm": best, "score": best * 100, "label": label, "sources": sources}


def _skill_external_intel(intel: dict | None) -> dict:
    if not intel:
        return {
            "norm": 0.45,
            "score": 45.0,
            "label": "intel unavailable",
            "confidence_multiplier": 0.9,
            "block_trade": False,
            "reasons": [],
        }
    freshness = _clamp(float(intel.get("data_freshness", 0.0)), 0.0, 1.0)
    velocity = _clamp(float(intel.get("news_velocity", 0.0)), 0.0, 1.0)
    sentiment = _clamp(abs(float(intel.get("news_sentiment_score", 0.0))), 0.0, 1.0)
    macro = _clamp(1.0 - abs(float(intel.get("macro_trend_score", 0.0))), 0.0, 1.0)
    norm = _clamp(freshness * 0.44 + velocity * 0.28 + sentiment * 0.18 + macro * 0.10, 0.0, 1.0)
    reasons = list(intel.get("reasons") or [])
    return {
        "norm": norm,
        "score": norm * 100,
        "label": _intel_label(norm, bool(intel.get("block_trade")), reasons),
        "confidence_multiplier": _clamp(float(intel.get("confidence_multiplier", 1.0)), 0.45, 1.05),
        "block_trade": bool(intel.get("block_trade", False)),
        "reasons": reasons,
    }


def _intel_label(norm: float, block_trade: bool, reasons: list[str]) -> str:
    if block_trade:
        return "blocked: weak external signal"
    if norm >= 0.7:
        return "strong external signal"
    if norm >= 0.4:
        return "moderate external signal"
    if reasons:
        return f"weak external signal ({reasons[0]})"
    return "weak external signal"


def _terms(text: str) -> set[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    stop = {
        "the", "and", "for", "with", "will", "this", "that", "from", "market",
        "kalshi", "contract", "contracts", "yes", "no", "above", "below",
    }
    return {w for w in cleaned.split() if len(w) >= 4 and w not in stop}


def _risk_prices(
    *,
    entry: float,
    edge_norm: float,
    liquidity_norm: float,
    time_quality: float,
    close_seconds: int,
) -> tuple[float, float, float, float]:
    base_move = 0.02 + edge_norm * 0.07 + liquidity_norm * 0.04
    tp_mult = _tp_time_multiplier(close_seconds)
    tp_move = round(_clamp(base_move * tp_mult, 0.02, 0.20), 2)
    rr_to_stop = 0.58 + (1.0 - liquidity_norm) * 0.15 + (1.0 - time_quality) * 0.12
    # Stop-loss policy: at least 4 cents, at most 40% of entry. The previous
    # logic let stops collapse to 1 cent on cheap entries, which got us
    # whipsawed out by single-tick noise. The floor is in absolute cents so
    # a $0.20 entry now risks 4-8¢ instead of 1¢.
    sl_floor = 0.04
    sl_ceiling = max(sl_floor, round(entry * 0.40, 2))
    sl_move = round(_clamp(tp_move * rr_to_stop, sl_floor, sl_ceiling), 2)
    target = round(_clamp(entry + tp_move, 0.01, 0.97), 2)
    stop = round(_clamp(entry - sl_move, 0.01, 0.96), 2)
    if stop >= entry:
        stop = round(max(0.01, entry - 0.01), 2)
        sl_move = round(entry - stop, 2)
    return target, stop, tp_move, sl_move


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _time_quality(close_seconds: int) -> float:
    """How favorable the remaining time is for fallback trades."""
    if close_seconds < 15 * 60:
        return 0.15
    if close_seconds < 60 * 60:
        return 0.55
    if close_seconds < 6 * 60 * 60:
        return 1.0
    if close_seconds < 24 * 60 * 60:
        return 0.75
    return 0.55


def _tp_time_multiplier(close_seconds: int) -> float:
    if close_seconds < 60 * 60:
        return 0.75
    if close_seconds < 6 * 60 * 60:
        return 1.0
    if close_seconds < 24 * 60 * 60:
        return 1.15
    return 1.25


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _claude_error_reason(exc: Exception) -> str:
    """Return a safe, actionable fallback reason without exposing secrets."""
    if isinstance(exc, AuthenticationError):
        return "claude unavailable: authentication failed; replace ANTHROPIC_API_KEY"
    if isinstance(exc, BadRequestError):
        return f"claude unavailable: bad request; {_api_error_message(exc)}"
    if isinstance(exc, RateLimitError):
        return "claude unavailable: rate limited"
    if isinstance(exc, APIConnectionError):
        return "claude unavailable: connection error"
    if isinstance(exc, APIStatusError):
        return f"claude unavailable: API status {exc.status_code}; {_api_error_message(exc)}"
    if isinstance(exc, RuntimeError) and "ANTHROPIC_API_KEY" in str(exc):
        return "claude unavailable: missing ANTHROPIC_API_KEY"
    return f"claude unavailable: {type(exc).__name__}"


def _api_error_message(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        else:
            message = str(body.get("message") or "")
    if not message:
        message = str(exc)
    # Keep database/UI text compact and avoid carrying request IDs or long payloads.
    return message.replace("\n", " ")[:180]


def check_claude_health() -> dict:
    """Small synchronous preflight used by the API health endpoint."""
    if settings.ANALYZER_PROVIDER.lower() != "claude":
        return {
            "ok": False,
            "enabled": False,
            "provider": settings.ANALYZER_PROVIDER,
            "model": settings.CLAUDE_MODEL,
            "error": "claude disabled; ANALYZER_PROVIDER=local prevents paid API calls",
        }
    if not settings.ANTHROPIC_API_KEY:
        return {
            "ok": False,
            "enabled": True,
            "provider": settings.ANALYZER_PROVIDER,
            "model": settings.CLAUDE_MODEL,
            "error": "missing ANTHROPIC_API_KEY",
        }
    try:
        msg = _claude().messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=16,
            messages=[{"role": "user", "content": "Return OK only."}],
        )
        text = "".join(
            b.text for b in msg.content if getattr(b, "type", "") == "text"
        ).strip()
        return {
            "ok": True,
            "enabled": True,
            "provider": settings.ANALYZER_PROVIDER,
            "model": settings.CLAUDE_MODEL,
            "response": text[:32],
        }
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "provider": settings.ANALYZER_PROVIDER,
            "model": settings.CLAUDE_MODEL,
            "error": _claude_error_reason(exc),
        }
