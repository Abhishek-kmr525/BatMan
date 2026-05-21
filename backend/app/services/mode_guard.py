"""Mode arming flow + kill switch for live trading controls (phase-3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from app.core.config import settings

Platform = Literal["kalshi", "polymarket", "candle"]
Mode = Literal["paper", "live_requested", "live_armed"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PlatformModeState:
    platform: Platform
    mode: Mode = "paper"
    kill_switch: bool = False
    requested_at: str | None = None
    armed_at: str | None = None
    last_changed_at: str = field(default_factory=_now_iso)
    limits: dict = field(default_factory=dict)

    @property
    def live_enabled(self) -> bool:
        if self.platform == "kalshi":
            return settings.KALSHI_LIVE_ENABLED
        if self.platform == "candle":
            return settings.CANDLE_LIVE_ENABLED
        return settings.POLYMARKET_LIVE_ENABLED

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "mode": self.mode,
            "live_enabled": self.live_enabled,
            "kill_switch": self.kill_switch,
            "requested_at": self.requested_at,
            "armed_at": self.armed_at,
            "last_changed_at": self.last_changed_at,
            "limits": self.limits,
        }


class ModeGuard:
    def __init__(self) -> None:
        self._state: dict[Platform, PlatformModeState] = {
            "kalshi": PlatformModeState(platform="kalshi"),
            "polymarket": PlatformModeState(platform="polymarket"),
            "candle": PlatformModeState(platform="candle"),
        }

    def get(self, platform: Platform) -> PlatformModeState:
        return self._state[platform]

    def snapshot(self) -> dict:
        return {k: v.to_dict() for k, v in self._state.items()}

    def request_live(self, platform: Platform) -> dict:
        s = self._state[platform]
        if not s.live_enabled:
            return {"ok": False, "error": f"{platform} live mode disabled by config"}
        if s.kill_switch:
            return {"ok": False, "error": "kill switch is active"}
        s.mode = "live_requested"
        s.requested_at = _now_iso()
        s.last_changed_at = s.requested_at
        return {"ok": True, "state": s.to_dict()}

    def confirm_live(self, platform: Platform, passcode: str, limits: dict | None) -> dict:
        s = self._state[platform]
        if s.mode != "live_requested":
            return {"ok": False, "error": "mode is not live_requested"}
        if passcode != settings.LIVE_MODE_CONFIRM_PASSCODE:
            return {"ok": False, "error": "invalid confirm passcode"}
        if s.kill_switch:
            return {"ok": False, "error": "kill switch is active"}
        s.mode = "live_armed"
        s.armed_at = _now_iso()
        s.last_changed_at = s.armed_at
        s.limits = limits or {}
        return {"ok": True, "state": s.to_dict()}

    def set_paper(self, platform: Platform) -> dict:
        s = self._state[platform]
        s.mode = "paper"
        s.requested_at = None
        s.armed_at = None
        s.last_changed_at = _now_iso()
        s.limits = {}
        return {"ok": True, "state": s.to_dict()}

    def set_kill_switch(self, platform: Platform, enabled: bool) -> dict:
        s = self._state[platform]
        s.kill_switch = bool(enabled)
        s.last_changed_at = _now_iso()
        if s.kill_switch:
            s.mode = "paper"
            s.requested_at = None
            s.armed_at = None
            s.limits = {}
        return {"ok": True, "state": s.to_dict()}

    def can_open_new_trade(self, platform: Platform) -> tuple[bool, str]:
        s = self._state[platform]
        if s.kill_switch:
            return False, "kill_switch_active"
        if s.mode == "live_requested":
            return False, "live_mode_pending_confirmation"
        # live_armed is allowed (execution adapter decides real/paper behavior).
        return True, "ok"


mode_guard = ModeGuard()

