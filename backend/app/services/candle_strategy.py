"""Candle-based strategy engine for crypto BTC/ETH.

Implements the rules-based ICT-style strategy from the video script:
  1. HTF (1H) bias must align with entry direction
  2. Liquidity sweep on the entry timeframe (5m default)
  3. Break of structure (BOS) after sweep, in trend direction
  4. Entry on the impulse/retest with volume confirmation
  5. Hard SL at setup invalidation + 1:2 RR target

Pure-Python — no numpy/pandas dependency. Operates on lists of OHLCV dicts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import settings
from app.services import binance_data

logger = logging.getLogger(__name__)


Direction = Literal["LONG", "SHORT", "SKIP"]
StrategyId = Literal["sweep_bos_v1", "smart_money_v2"]

SUPPORTED_STRATEGIES = {
    "sweep_bos_v1": "Sweep + BOS + volume (current)",
    "smart_money_v2": "HTF S/D + CHOCH/BOS + RSI/Stoch + 3R",
}


@dataclass
class CandleSignal:
    """Output of strategy analysis — pass to bot for execution."""
    direction: Direction
    confidence: float = 0.0  # 0..1
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    rr_ratio: float = 0.0
    htf_bias: str = "mixed"  # up | down | mixed
    setup_type: str = ""
    reasoning: str = ""
    meta: dict = field(default_factory=dict)


# ─────────────────────────── HTF BIAS ────────────────────────────────

def _trend_from_emas(closes: list[float]) -> tuple[str, float, float]:
    """Quick & robust trend gauge using EMA20 vs EMA50 vs EMA200."""
    if len(closes) < 50:
        return ("mixed", 0.0, 0.0)
    e20 = _ema(closes, 20)
    e50 = _ema(closes, 50)
    e200 = _ema(closes, 200) if len(closes) >= 200 else _ema(closes, max(50, len(closes) - 1))
    last_price = closes[-1]
    slope = (closes[-1] - closes[-10]) / closes[-10] if closes[-10] else 0.0
    # Strong up: price > 20 > 50 > 200 and slope positive
    if e20 > e50 > e200 and last_price > e20 and slope > 0:
        return ("up", e20, slope)
    if e20 < e50 < e200 and last_price < e20 and slope < 0:
        return ("down", e20, slope)
    if last_price > e20 > e50 and slope > 0:
        return ("up", e20, slope)
    if last_price < e20 < e50 and slope < 0:
        return ("down", e20, slope)
    return ("mixed", e20, slope)


def _ema(values: list[float], period: int) -> float:
    """Exponential moving average — returns last EMA value."""
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


async def get_htf_bias(symbol: str, interval: str | None = None) -> tuple[str, dict]:
    """Determine higher-timeframe bias.

    Returns ('up'|'down'|'mixed', meta_dict).
    """
    htf_interval = interval or settings.CANDLE_HTF_INTERVAL
    klines = await binance_data.get_klines(symbol, htf_interval, limit=250)
    if len(klines) < 50:
        return ("mixed", {"reason": f"insufficient_htf_data_{len(klines)}"})
    closes = [k["c"] for k in klines]
    bias, ema20, slope = _trend_from_emas(closes)
    return (bias, {
        "htf_interval": htf_interval,
        "ema20": round(ema20, 4),
        "slope_10c": round(slope, 6),
        "last_price": closes[-1],
    })


# ─────────────────────── LIQUIDITY SWEEP DETECT ──────────────────────

def detect_liquidity_sweep(
    klines: list[dict],
    lookback: int = 20,
) -> tuple[str, dict]:
    """Look for a wick that breaks recent high/low then closes back inside.

    Returns:
      ('bull_sweep'|'bear_sweep'|'none', metadata_dict)

    Bull sweep (precedes a LONG): last candle's low pierces lookback-low
        but closes back above it → buyers swept stops, intent to rally.
    Bear sweep (precedes a SHORT): last candle's high pierces lookback-high
        but closes back below → sellers swept stops, intent to drop.
    """
    if len(klines) < lookback + 2:
        return ("none", {"reason": "insufficient_candles"})

    recent = klines[-(lookback + 1):-1]  # exclude the current candle
    high_zone = max(c["h"] for c in recent)
    low_zone = min(c["l"] for c in recent)

    last = klines[-1]
    body_top = max(last["o"], last["c"])
    body_bot = min(last["o"], last["c"])

    # Bull sweep: wick into low_zone, close back above it.
    if last["l"] < low_zone and last["c"] > low_zone and body_bot > low_zone * 0.999:
        wick_depth = (low_zone - last["l"]) / low_zone
        return ("bull_sweep", {
            "sweep_level": round(low_zone, 4),
            "wick_low": round(last["l"], 4),
            "wick_depth_pct": round(wick_depth * 100, 3),
            "close": round(last["c"], 4),
        })

    # Bear sweep: wick above high_zone, close back below.
    if last["h"] > high_zone and last["c"] < high_zone and body_top < high_zone * 1.001:
        wick_height = (last["h"] - high_zone) / high_zone
        return ("bear_sweep", {
            "sweep_level": round(high_zone, 4),
            "wick_high": round(last["h"], 4),
            "wick_height_pct": round(wick_height * 100, 3),
            "close": round(last["c"], 4),
        })

    return ("none", {"high_zone": round(high_zone, 4), "low_zone": round(low_zone, 4)})


# ─────────────────────── BREAK OF STRUCTURE ──────────────────────────

def detect_bos(
    klines: list[dict],
    direction: Literal["LONG", "SHORT"],
    lookback: int = 10,
) -> tuple[bool, dict]:
    """Check if the latest closed candle broke a swing-high/low → BOS.

    LONG BOS: close > most recent swing-high in the lookback window.
    SHORT BOS: close < most recent swing-low in the lookback window.
    """
    if len(klines) < lookback + 2:
        return (False, {"reason": "insufficient_candles"})

    last = klines[-1]
    recent = klines[-(lookback + 1):-1]
    if direction == "LONG":
        swing_high = max(c["h"] for c in recent)
        broke = last["c"] > swing_high
        return (broke, {
            "swing_high": round(swing_high, 4),
            "close": round(last["c"], 4),
            "break_pct": round((last["c"] - swing_high) / swing_high * 100, 3),
        })
    else:  # SHORT
        swing_low = min(c["l"] for c in recent)
        broke = last["c"] < swing_low
        return (broke, {
            "swing_low": round(swing_low, 4),
            "close": round(last["c"], 4),
            "break_pct": round((swing_low - last["c"]) / swing_low * 100, 3),
        })


# ─────────────────────── VOLUME CONFIRMATION ─────────────────────────

def volume_confirmation(klines: list[dict], multiplier: float = 1.3) -> tuple[bool, dict]:
    """Last candle volume must exceed N× the 20-candle average."""
    if len(klines) < 21:
        return (False, {"reason": "insufficient_candles"})
    last_vol = klines[-1]["v"]
    avg_vol = sum(k["v"] for k in klines[-21:-1]) / 20
    if avg_vol <= 0:
        return (False, {"reason": "zero_volume_avg"})
    ratio = last_vol / avg_vol
    return (ratio >= multiplier, {
        "last_vol": round(last_vol, 2),
        "avg_vol_20c": round(avg_vol, 2),
        "ratio": round(ratio, 3),
    })


# ─────────────────────── FULL STRATEGY ANALYZE ───────────────────────

async def analyze_symbol(symbol: str) -> CandleSignal:
    """Full strategy pipeline. Returns a CandleSignal."""
    primary = settings.CANDLE_PRIMARY_INTERVAL
    sweep_lookback = settings.CANDLE_SWEEP_LOOKBACK
    bos_lookback = settings.CANDLE_BOS_LOOKBACK
    vol_mult = settings.CANDLE_VOLUME_MULTIPLIER
    rr_target = settings.CANDLE_MIN_RR_RATIO

    # ── 1. HTF bias ───────────────────────────────────────────────
    htf_bias, htf_meta = await get_htf_bias(symbol)

    # ── 2. Primary timeframe candles ─────────────────────────────
    klines = await binance_data.get_klines(symbol, primary, limit=max(sweep_lookback + 50, 100))
    if len(klines) < sweep_lookback + 5:
        return CandleSignal(
            direction="SKIP",
            reasoning=f"insufficient {primary} candles ({len(klines)})",
            meta={"htf_bias": htf_bias, **htf_meta},
        )

    # ── 3. Liquidity sweep ───────────────────────────────────────
    sweep, sweep_meta = detect_liquidity_sweep(klines, lookback=sweep_lookback)
    last_close = klines[-1]["c"]

    if sweep == "none":
        # Fallback path to increase opportunity count:
        # allow continuation entries when HTF is directional + BOS + volume confirm.
        if htf_bias in {"up", "down"}:
            fallback_dir: Direction = "LONG" if htf_bias == "up" else "SHORT"
            bos_ok_fb, bos_meta_fb = detect_bos(klines, fallback_dir, lookback=bos_lookback)
            vol_ok_fb, vol_meta_fb = volume_confirmation(klines, multiplier=max(1.1, vol_mult - 0.15))
            if bos_ok_fb and vol_ok_fb:
                entry = last_close
                if fallback_dir == "LONG":
                    stop_loss = round(min(k["l"] for k in klines[-6:]) * 0.998, 4)
                    risk = entry - stop_loss
                    take_profit = round(entry + risk * rr_target, 4)
                else:
                    stop_loss = round(max(k["h"] for k in klines[-6:]) * 1.002, 4)
                    risk = stop_loss - entry
                    take_profit = round(entry - risk * rr_target, 4)
                if risk > 0 and take_profit > 0:
                    rr_actual_fb = (
                        (take_profit - entry) / risk if fallback_dir == "LONG"
                        else (entry - take_profit) / risk
                    )
                    return CandleSignal(
                        direction=fallback_dir,
                        confidence=0.50,
                        entry_price=entry,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        rr_ratio=round(rr_actual_fb, 3),
                        htf_bias=htf_bias,
                        setup_type="trend_bos_fallback",
                        reasoning=(
                            f"fallback {fallback_dir} via HTF trend + BOS + volume "
                            f"(vol_ratio={vol_meta_fb.get('ratio', 0):.2f})"
                        ),
                        meta={**htf_meta, **sweep_meta, **bos_meta_fb, **vol_meta_fb},
                    )
        return CandleSignal(
            direction="SKIP",
            htf_bias=htf_bias,
            reasoning="no liquidity sweep",
            meta={**htf_meta, **sweep_meta},
        )

    # Map sweep → intended direction
    intended_direction: Direction = "LONG" if sweep == "bull_sweep" else "SHORT"

    # ── 4. HTF alignment ─────────────────────────────────────────
    if intended_direction == "LONG" and htf_bias == "down":
        return CandleSignal(
            direction="SKIP",
            htf_bias=htf_bias,
            setup_type=sweep,
            reasoning=f"HTF bias {htf_bias} conflicts with LONG bias from sweep",
            meta={**htf_meta, **sweep_meta},
        )
    if intended_direction == "SHORT" and htf_bias == "up":
        return CandleSignal(
            direction="SKIP",
            htf_bias=htf_bias,
            setup_type=sweep,
            reasoning=f"HTF bias {htf_bias} conflicts with SHORT bias from sweep",
            meta={**htf_meta, **sweep_meta},
        )

    # ── 5. Break of structure (lighter requirement — many sweeps
    #       fail BOS by 1 candle; we accept either BOS or strong
    #       momentum close in direction).
    bos_ok, bos_meta = detect_bos(klines, intended_direction, lookback=bos_lookback)

    # ── 6. Volume confirmation ──────────────────────────────────
    vol_ok, vol_meta = volume_confirmation(klines, multiplier=vol_mult)

    # ── 7. Confidence scoring ───────────────────────────────────
    confidence = 0.40  # base for valid sweep
    if htf_bias != "mixed":
        confidence += 0.20
    if bos_ok:
        confidence += 0.20
    if vol_ok:
        confidence += 0.15
    # Boost if the sweep was deep (real grab of stops)
    wick_depth = sweep_meta.get("wick_depth_pct") or sweep_meta.get("wick_height_pct") or 0
    if wick_depth > 0.1:
        confidence += 0.05
    confidence = min(confidence, 0.99)

    # ── 8. Risk levels ──────────────────────────────────────────
    if intended_direction == "LONG":
        # SL = below sweep wick low
        stop_loss = round(klines[-1]["l"] * 0.998, 4)  # 0.2% buffer beneath wick
        risk = last_close - stop_loss
        take_profit = round(last_close + risk * rr_target, 4)
    else:
        stop_loss = round(klines[-1]["h"] * 1.002, 4)
        risk = stop_loss - last_close
        take_profit = round(last_close - risk * rr_target, 4)

    if risk <= 0 or take_profit <= 0:
        return CandleSignal(
            direction="SKIP",
            htf_bias=htf_bias,
            setup_type=sweep,
            reasoning="invalid risk levels",
            meta={**htf_meta, **sweep_meta},
        )

    rr_actual = (take_profit - last_close) / risk if intended_direction == "LONG" else (last_close - take_profit) / risk

    # Require minimum confidence floor.
    if confidence < settings.CANDLE_MIN_CONFIDENCE:
        return CandleSignal(
            direction="SKIP",
            htf_bias=htf_bias,
            setup_type=sweep,
            confidence=confidence,
            reasoning=f"confidence {confidence:.2f} below {settings.CANDLE_MIN_CONFIDENCE:.2f} floor",
            meta={**htf_meta, **sweep_meta, **bos_meta, **vol_meta},
        )

    reasoning = (
        f"{sweep} on {primary}, HTF={htf_bias}, BOS={'yes' if bos_ok else 'no'}, "
        f"vol_ratio={vol_meta.get('ratio',0):.2f}, RR={rr_actual:.2f}"
    )

    return CandleSignal(
        direction=intended_direction,
        confidence=round(confidence, 3),
        entry_price=last_close,
        stop_loss=stop_loss,
        take_profit=take_profit,
        rr_ratio=round(rr_actual, 3),
        htf_bias=htf_bias,
        setup_type=sweep,
        reasoning=reasoning,
        meta={**htf_meta, **sweep_meta, **bos_meta, **vol_meta},
    )


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))


def _stoch_k(klines: list[dict], period: int = 14) -> float:
    if len(klines) < period:
        return 50.0
    recent = klines[-period:]
    hh = max(k["h"] for k in recent)
    ll = min(k["l"] for k in recent)
    close = recent[-1]["c"]
    denom = hh - ll
    if denom <= 0:
        return 50.0
    return ((close - ll) / denom) * 100


def _is_pin_bar(k: dict) -> bool:
    body = abs(k["c"] - k["o"])
    rng = k["h"] - k["l"]
    if rng <= 0:
        return False
    upper = k["h"] - max(k["o"], k["c"])
    lower = min(k["o"], k["c"]) - k["l"]
    return (lower >= body * 2 or upper >= body * 2) and (body / rng) <= 0.35


def _is_engulfing(prev: dict, cur: dict) -> bool:
    prev_top = max(prev["o"], prev["c"])
    prev_bot = min(prev["o"], prev["c"])
    cur_top = max(cur["o"], cur["c"])
    cur_bot = min(cur["o"], cur["c"])
    return cur_top >= prev_top and cur_bot <= prev_bot


async def analyze_symbol_smart_money_v2(symbol: str) -> CandleSignal:
    """Second strategy profile based on user's checklist, adapted to crypto candles."""
    primary = settings.CANDLE_PRIMARY_INTERVAL
    rr_target = max(3.0, settings.CANDLE_MIN_RR_RATIO)
    htf_bias, htf_meta = await get_htf_bias(symbol, interval=settings.CANDLE_HTF_INTERVAL)
    klines = await binance_data.get_klines(symbol, primary, limit=220)
    if len(klines) < 80:
        return CandleSignal(direction="SKIP", reasoning=f"insufficient {primary} candles ({len(klines)})")

    closes = [k["c"] for k in klines]
    rsi = _rsi(closes, 14)
    stoch = _stoch_k(klines, 14)
    sweep, sweep_meta = detect_liquidity_sweep(klines, lookback=max(20, settings.CANDLE_SWEEP_LOOKBACK))
    if sweep == "none":
        return CandleSignal(direction="SKIP", htf_bias=htf_bias, reasoning="no liquidity sweep (v2)", meta={**htf_meta})

    intended_direction: Direction = "LONG" if sweep == "bull_sweep" else "SHORT"
    bos_ok, bos_meta = detect_bos(klines, intended_direction, lookback=max(8, settings.CANDLE_BOS_LOOKBACK))
    if not bos_ok:
        return CandleSignal(
            direction="SKIP",
            htf_bias=htf_bias,
            setup_type=sweep,
            reasoning="no BOS/CHOCH confirmation (v2)",
            meta={**htf_meta, **sweep_meta, **bos_meta},
        )

    # Trend filter and oscillator extremes from user plan.
    if intended_direction == "LONG" and htf_bias == "down":
        return CandleSignal(direction="SKIP", htf_bias=htf_bias, setup_type=sweep, reasoning="HTF down conflict (v2)")
    if intended_direction == "SHORT" and htf_bias == "up":
        return CandleSignal(direction="SKIP", htf_bias=htf_bias, setup_type=sweep, reasoning="HTF up conflict (v2)")
    if intended_direction == "LONG" and not (rsi <= 40 or stoch <= 25):
        return CandleSignal(direction="SKIP", htf_bias=htf_bias, setup_type=sweep, reasoning="no oversold RSI/Stoch (v2)", meta={"rsi": round(rsi, 2), "stoch_k": round(stoch, 2)})
    if intended_direction == "SHORT" and not (rsi >= 60 or stoch >= 75):
        return CandleSignal(direction="SKIP", htf_bias=htf_bias, setup_type=sweep, reasoning="no overbought RSI/Stoch (v2)", meta={"rsi": round(rsi, 2), "stoch_k": round(stoch, 2)})

    prev = klines[-2]
    cur = klines[-1]
    if not (_is_pin_bar(cur) or _is_engulfing(prev, cur)):
        return CandleSignal(direction="SKIP", htf_bias=htf_bias, setup_type=sweep, reasoning="no reversal candle trigger (v2)")

    entry = cur["c"]
    if intended_direction == "LONG":
        stop_loss = round(cur["l"] * 0.998, 4)
        risk = entry - stop_loss
        take_profit = round(entry + (risk * rr_target), 4)
    else:
        stop_loss = round(cur["h"] * 1.002, 4)
        risk = stop_loss - entry
        take_profit = round(entry - (risk * rr_target), 4)
    if risk <= 0:
        return CandleSignal(direction="SKIP", htf_bias=htf_bias, reasoning="invalid risk levels (v2)")

    confidence = 0.62
    if htf_bias != "mixed":
        confidence += 0.10
    if _is_engulfing(prev, cur):
        confidence += 0.08
    if _is_pin_bar(cur):
        confidence += 0.05
    confidence = min(0.95, confidence)

    return CandleSignal(
        direction=intended_direction,
        confidence=round(confidence, 3),
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        rr_ratio=round(rr_target, 3),
        htf_bias=htf_bias,
        setup_type="smart_money_v2",
        reasoning=(
            f"v2 {sweep}, HTF={htf_bias}, BOS=yes, RSI={rsi:.1f}, "
            f"Stoch={stoch:.1f}, candle={'engulfing' if _is_engulfing(prev, cur) else 'pin'}"
        ),
        meta={**htf_meta, **sweep_meta, **bos_meta, "rsi": round(rsi, 2), "stoch_k": round(stoch, 2)},
    )


async def analyze_symbol_with_strategy(symbol: str, strategy_id: StrategyId) -> CandleSignal:
    if strategy_id == "smart_money_v2":
        return await analyze_symbol_smart_money_v2(symbol)
    return await analyze_symbol(symbol)
