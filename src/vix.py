
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)

INDIA_VIX_TICKER = "^INDIAVIX"


# ── VIX Zone ───────────────────────────────────────────────────────────────────

class VixZone(str, Enum):
    """
    Classifies India VIX into four trading zones.
    Each zone implies different market behaviour and alert confidence.

    CALM     < 15   : Low fear, reliable signals, trends sustained
    NORMAL   15-20  : Healthy volatility, thresholds well calibrated
    ELEVATED 20-30  : Heightened fear, whipsaws possible, reduce size
    PANIC    > 30   : Extreme fear, avoid new positions, protect capital
    """
    CALM     = "CALM"
    NORMAL   = "NORMAL"
    ELEVATED = "ELEVATED"
    PANIC    = "PANIC"


# ── VIX Snapshot ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VixSnapshot:
    """
    Immutable container for one India VIX reading.
    Created by VixFetcher, used by formatters and alert generators.
    """
    value:      float
    prev_close: float
    change_pct: float
    zone:       VixZone
    fetched_at: datetime

    @property
    def sign(self) -> str:
        return "+" if self.change_pct >= 0 else ""

    @property
    def change_arrow(self) -> str:
        return "▲" if self.change_pct >= 0 else "▼"

    @property
    def zone_emoji(self) -> str:
        return {
            VixZone.CALM:     "🟢",
            VixZone.NORMAL:   "🟡",
            VixZone.ELEVATED: "🟠",
            VixZone.PANIC:    "🔴",
        }[self.zone]

    @property
    def zone_label(self) -> str:
        return {
            VixZone.CALM:     "Calm — low fear",
            VixZone.NORMAL:   "Normal",
            VixZone.ELEVATED: "Elevated — caution",
            VixZone.PANIC:    "Panic — extreme fear",
        }[self.zone]

    def is_spiking(self, threshold_pct: float = 15.0) -> bool:
        """
        True if VIX rose more than threshold_pct% from previous close.
        A VIX spike almost always precedes a sharp Nifty sell-off.
        """
        return self.change_pct >= threshold_pct

    def ist_time_str(self) -> str:
        from zoneinfo import ZoneInfo
        return self.fetched_at.astimezone(
            ZoneInfo("Asia/Kolkata")
        ).strftime("%d %b %Y, %I:%M %p IST")


# ── VIX Fetcher ────────────────────────────────────────────────────────────────

class VixFetcher:
    """
    Fetches India VIX from Yahoo Finance.
    Free — no API key required.
    Returns VixSnapshot or None on failure.
    """

    def fetch(self) -> Optional[VixSnapshot]:
        """
        Downloads latest India VIX and previous close.
        Falls back to 3-day history if fast_info is unavailable.
        """
        try:
            ticker = yf.Ticker(INDIA_VIX_TICKER)

            # Attempt fast_info first (lightweight call)
            value      = 0.0
            prev_close = 0.0
            try:
                info       = ticker.fast_info
                value      = float(info.last_price   or 0)
                prev_close = float(info.previous_close or 0)
            except Exception:
                pass

            # Fallback to daily history
            if value <= 0 or prev_close <= 0:
                hist = ticker.history(period="3d", interval="1d")
                if hist is None or len(hist) < 2:
                    logger.warning("India VIX: insufficient history data")
                    return None
                value      = float(hist["Close"].iloc[-1])
                prev_close = float(hist["Close"].iloc[-2])

            if value <= 0:
                logger.warning("India VIX: invalid value %.2f", value)
                return None

            change_pct = ((value - prev_close) / prev_close) * 100
            zone       = VixInterpreter.classify(value)

            snap = VixSnapshot(
                value=round(value, 2),
                prev_close=round(prev_close, 2),
                change_pct=round(change_pct, 2),
                zone=zone,
                fetched_at=datetime.now(timezone.utc),
            )
            logger.info(
                "India VIX: %.2f (%s%+.2f%%) zone=%s",
                snap.value, snap.sign, snap.change_pct, snap.zone.value,
            )
            return snap

        except Exception as exc:
            logger.error(
                "India VIX fetch failed: %s", exc, exc_info=True
            )
            return None


# ── VIX Interpreter ────────────────────────────────────────────────────────────

class VixInterpreter:
    """
    Classifies VIX levels and generates plain-English trading context
    embedded in alert emails and morning briefings.
    """

    CALM_MAX     = 15.0
    NORMAL_MAX   = 20.0
    ELEVATED_MAX = 30.0

    @staticmethod
    def classify(value: float) -> VixZone:
        if value < VixInterpreter.CALM_MAX:
            return VixZone.CALM
        elif value < VixInterpreter.NORMAL_MAX:
            return VixZone.NORMAL
        elif value < VixInterpreter.ELEVATED_MAX:
            return VixZone.ELEVATED
        else:
            return VixZone.PANIC

    @staticmethod
    def get_market_context(snap: VixSnapshot) -> str:
        """
        One-sentence trading context based on VIX zone.
        Embedded in every alert email body.
        """
        contexts = {
            VixZone.CALM: (
                f"VIX {snap.value:.1f} (calm) — market fear is low, "
                f"S/R signals are high confidence."
            ),
            VixZone.NORMAL: (
                f"VIX {snap.value:.1f} (normal) — standard "
                f"conditions, alert signals are reliable."
            ),
            VixZone.ELEVATED: (
                f"VIX {snap.value:.1f} (elevated) — heightened fear, "
                f"confirm signals before acting, consider smaller size."
            ),
            VixZone.PANIC: (
                f"VIX {snap.value:.1f} (PANIC ZONE) — extreme "
                f"volatility. Avoid new positions. Protect capital."
            ),
        }
        return contexts[snap.zone]

    @staticmethod
    def get_threshold_advice(snap: VixSnapshot) -> str:
        """Threshold adjustment suggestion for morning briefing."""
        advice = {
            VixZone.CALM: (
                "VIX is low — consider lowering threshold to 0.3% "
                "to catch smaller moves."
            ),
            VixZone.NORMAL: (
                "Thresholds (0.5% normal, 1.5% critical) are well "
                "calibrated for this VIX level."
            ),
            VixZone.ELEVATED: (
                "VIX elevated — small moves are noise. Consider "
                "raising threshold to 0.8% to reduce false alerts."
            ),
            VixZone.PANIC: (
                "VIX in panic zone — ALL critical alerts are genuine "
                "and urgent. Every alert needs immediate attention."
            ),
        }
        return advice[snap.zone]

    @staticmethod
    def get_spike_context(snap: VixSnapshot) -> str:
        """Context string for VIX spike dedicated alert."""
        return (
            f"India VIX has spiked {snap.sign}{snap.change_pct:.1f}% "
            f"from {snap.prev_close:.1f} to {snap.value:.1f}. "
            f"A VIX spike of this magnitude almost always precedes "
            f"a sharp Nifty 50 sell-off within the same or next session. "
            f"Review all open positions immediately."
        )


# ── VIX Alert Checker ──────────────────────────────────────────────────────────

class VixAlertChecker:
    """
    Monitors India VIX for sudden spikes every poll cycle.

    Alert fires when VIX rises more than 15% from previous close
    in a single session — a reliable leading indicator of Nifty falls.

    Has its own 60-minute cooldown independent of price alerts.
    """

    SPIKE_THRESHOLD_PCT = 15.0
    COOLDOWN_MINUTES    = 60

    def __init__(self) -> None:
        self._last_alert_time: Optional[datetime] = None

    def should_alert(self, snap: VixSnapshot) -> bool:
        """
        Returns True if a VIX spike alert should fire now.
        Handles cooldown internally.
        """
        if not snap.is_spiking(self.SPIKE_THRESHOLD_PCT):
            return False

        now = datetime.now(timezone.utc)
        if self._last_alert_time is not None:
            elapsed_min = (
                now - self._last_alert_time
            ).total_seconds() / 60
            if elapsed_min < self.COOLDOWN_MINUTES:
                logger.debug(
                    "VIX spike suppressed — cooldown "
                    "(%.0f min remaining)",
                    self.COOLDOWN_MINUTES - elapsed_min,
                )
                return False

        self._last_alert_time = now
        logger.warning(
            "VIX SPIKE detected: %.2f (%+.1f%%) — alert firing",
            snap.value, snap.change_pct,
        )
        return True

    def format_spike_email_subject(self, snap: VixSnapshot) -> str:
        return (
            f"[🚨 VIX SPIKE] India VIX {snap.sign}"
            f"{snap.change_pct:.1f}% "
            f"→ {snap.value:.1f} | Nifty danger signal"
        )

    def format_spike_email_body(self, snap: VixSnapshot) -> str:
        sep = "=" * 45
        return f"""
{sep}
  INDIA VIX SPIKE ALERT
{sep}

  India VIX    : {snap.value:.2f}
  Previous     : {snap.prev_close:.2f}
  Change       : {snap.sign}{snap.change_pct:.2f}% {snap.change_arrow}
  Zone         : {snap.zone_emoji} {snap.zone_label}
  Alert Time   : {snap.ist_time_str()}

{sep}
  WHAT THIS MEANS
{sep}
  {VixInterpreter.get_spike_context(snap)}

{sep}
  RECOMMENDED ACTIONS
{sep}
  - Review all open Nifty/Sensex positions immediately
  - Tighten stop-losses on long positions
  - Avoid entering new long positions until VIX stabilises
  - Watch for Nifty to test nearest support level (S1/S2)

{sep}
This is an automated VIX Spike Alert from your
Stock Alert System.
{sep}
        """.strip()

    def format_spike_telegram(self, snap: VixSnapshot) -> str:
        return (
            f"🚨 <b>VIX SPIKE ALERT</b> 🚨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"<b>India VIX</b> : <code>{snap.value:.2f}</code>\n"
            f"<b>Change</b>    : "
            f"<code>{snap.sign}{snap.change_pct:.1f}% "
            f"{snap.change_arrow}</code>\n"
            f"<b>Previous</b>  : <code>{snap.prev_close:.2f}</code>\n"
            f"<b>Zone</b>      : {snap.zone_emoji} {snap.zone_label}\n"
            f"\n"
            f"⚠️ <b>VIX spike = imminent Nifty sell-off signal</b>\n"
            f"Review all positions immediately.\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Tighten stops. Avoid new longs.</i>"
        )