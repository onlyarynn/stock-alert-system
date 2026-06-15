"""
levels.py
---------
Support and Resistance level detection using Pivot Points.

Calculates daily S/R levels from historical price data automatically
every morning. No manual configuration needed.

Method: Classic Pivot Points (floor trader pivots)
  PP  = (High + Low + Close) / 3
  R1  = (2 × PP) - Low
  R2  = PP + (High - Low)
  R3  = High + 2 × (PP - Low)
  S1  = (2 × PP) - High
  S2  = PP - (High - Low)
  S3  = Low - 2 × (High - PP)

Two alert types:
  APPROACHING — price within proximity_pct% of a level
  BREAKOUT    — price has crossed through a level
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import yfinance as yf

from .config import TICKER_DISPLAY_NAMES, get_settings

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────────────────

class LevelType(str, Enum):
    SUPPORT    = "SUPPORT"
    RESISTANCE = "RESISTANCE"
    PIVOT      = "PIVOT"


class LevelAlertType(str, Enum):
    APPROACHING = "APPROACHING"
    BREAKOUT    = "BREAKOUT"
    BREAKDOWN   = "BREAKDOWN"


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PivotLevels:
    """
    Holds all calculated pivot point levels for one ticker.
    Calculated once per trading day from previous day's OHLC data.
    """
    ticker:       str
    display_name: str
    pivot:        float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    calculated_on: datetime
    prev_high:    float
    prev_low:     float
    prev_close:   float

    def all_levels(self) -> dict[str, tuple[float, LevelType]]:
        """Returns all levels as {name: (price, type)} dict."""
        return {
            "R3": (self.r3, LevelType.RESISTANCE),
            "R2": (self.r2, LevelType.RESISTANCE),
            "R1": (self.r1, LevelType.RESISTANCE),
            "PP": (self.pivot, LevelType.PIVOT),
            "S1": (self.s1, LevelType.SUPPORT),
            "S2": (self.s2, LevelType.SUPPORT),
            "S3": (self.s3, LevelType.SUPPORT),
        }

    def nearest_resistance(self, current_price: float) -> Optional[tuple[str, float]]:
        """Returns (name, price) of the nearest resistance level above current price."""
        levels = [
            (name, price) for name, (price, ltype) in self.all_levels().items()
            if ltype == LevelType.RESISTANCE and price > current_price
        ]
        return min(levels, key=lambda x: x[1]) if levels else None

    def nearest_support(self, current_price: float) -> Optional[tuple[str, float]]:
        """Returns (name, price) of the nearest support level below current price."""
        levels = [
            (name, price) for name, (price, ltype) in self.all_levels().items()
            if ltype == LevelType.SUPPORT and price < current_price
        ]
        return max(levels, key=lambda x: x[1]) if levels else None


@dataclass(frozen=True)
class LevelAlert:
    """
    Fired when price approaches or breaks through a support/resistance level.
    Passed to the notifier to format and send the email.
    """
    ticker:        str
    display_name:  str
    alert_type:    LevelAlertType
    level_name:    str
    level_type:    LevelType
    level_price:   float
    current_price: float
    distance_pct:  float
    levels:        PivotLevels
    generated_at:  datetime

    @property
    def emoji(self) -> str:
        if self.alert_type == LevelAlertType.BREAKOUT:
            return "🟢"
        if self.alert_type == LevelAlertType.BREAKDOWN:
            return "🔴"
        return "⚠️"

    @property
    def action_hint(self) -> str:
        """Trading context hint based on alert type and level type."""
        if self.alert_type == LevelAlertType.BREAKOUT:
            return (
                "Price has broken ABOVE resistance. "
                "Possible bullish continuation — consider buy/hold."
            )
        if self.alert_type == LevelAlertType.BREAKDOWN:
            return (
                "Price has broken BELOW support. "
                "Possible bearish continuation — consider sell/hedge."
            )
        if self.level_type == LevelType.RESISTANCE:
            return (
                f"Price approaching resistance {self.level_name}. "
                "Watch for rejection or breakout."
            )
        return (
            f"Price approaching support {self.level_name}. "
            "Watch for bounce or breakdown."
        )

    def format_email_subject(self) -> str:
        sign = "+" if self.current_price >= self.level_price else "-"
        if self.alert_type == LevelAlertType.APPROACHING:
            return (
                f"[⚠️ APPROACHING {self.level_type.value}] "
                f"{self.display_name} near {self.level_name} "
                f"({self.level_price:,.2f}) | "
                f"Now: {self.current_price:,.2f}"
            )
        return (
            f"[{self.emoji} {self.alert_type.value}] "
            f"{self.display_name} broke {self.level_name} "
            f"({self.level_price:,.2f}) | "
            f"Now: {self.current_price:,.2f}"
        )

    def format_email_body(self) -> str:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        ist_time = self.generated_at.astimezone(ist)
        time_str = ist_time.strftime("%d %b %Y, %I:%M %p IST")
        sep = "=" * 45
        lvls = self.levels

        return f"""
{sep}
  {self.emoji} {self.alert_type.value} ALERT — {self.display_name}
{sep}

  Alert Type    : {self.emoji} {self.alert_type.value}
  Level         : {self.level_name} ({self.level_type.value})
  Level Price   : {self.level_price:,.2f}
  Current Price : {self.current_price:,.2f}
  Distance      : {abs(self.distance_pct):.3f}% from level
  Alert Time    : {time_str}

{sep}
  ACTION HINT
{sep}
  {self.action_hint}

{sep}
  TODAY'S PIVOT LEVELS (for reference)
{sep}
  Resistance 3  : {lvls.r3:>10,.2f}
  Resistance 2  : {lvls.r2:>10,.2f}
  Resistance 1  : {lvls.r1:>10,.2f}
  ── Pivot Point : {lvls.pivot:>10,.2f} ──
  Support 1     : {lvls.s1:>10,.2f}
  Support 2     : {lvls.s2:>10,.2f}
  Support 3     : {lvls.s3:>10,.2f}

  Previous Day  : H={lvls.prev_high:,.2f}  L={lvls.prev_low:,.2f}  C={lvls.prev_close:,.2f}

{sep}
This is an automated alert from your Stock Alert System.
Proximity threshold: 0.3% | Method: Classic Pivot Points
{sep}
        """.strip()


# ── Pivot Calculator ───────────────────────────────────────────────────────────

class PivotCalculator:
    """
    Calculates daily pivot point levels from historical OHLC data.
    Fetches previous trading day's High, Low, Close from yFinance.
    """

    def calculate(self, ticker: str) -> Optional[PivotLevels]:
        """
        Fetch last 5 days of data and calculate pivot levels
        from the most recent completed trading day.
        Returns PivotLevels or None if data unavailable.
        """
        try:
            t  = yf.Ticker(ticker)
            df = t.history(period="5d", interval="1d", auto_adjust=True)

            if df is None or len(df) < 2:
                logger.warning(
                    "[%s] Not enough historical data for pivot calculation",
                    ticker
                )
                return None

            # Use the last COMPLETED day (second-to-last row)
            # The last row may be today's incomplete data
            prev = df.iloc[-2]
            high  = float(prev["High"])
            low   = float(prev["Low"])
            close = float(prev["Close"])

            if high <= 0 or low <= 0 or close <= 0:
                logger.warning(
                    "[%s] Invalid OHLC data: H=%.2f L=%.2f C=%.2f",
                    ticker, high, low, close
                )
                return None

            # Classic Pivot Point formula
            pp = (high + low + close) / 3
            r1 = (2 * pp) - low
            r2 = pp + (high - low)
            r3 = high + 2 * (pp - low)
            s1 = (2 * pp) - high
            s2 = pp - (high - low)
            s3 = low - 2 * (high - pp)

            levels = PivotLevels(
                ticker=ticker,
                display_name=TICKER_DISPLAY_NAMES.get(ticker, ticker),
                pivot=round(pp, 2),
                r1=round(r1, 2),
                r2=round(r2, 2),
                r3=round(r3, 2),
                s1=round(s1, 2),
                s2=round(s2, 2),
                s3=round(s3, 2),
                calculated_on=datetime.now(timezone.utc),
                prev_high=round(high, 2),
                prev_low=round(low, 2),
                prev_close=round(close, 2),
            )

            logger.info(
                "[%s] Pivot levels calculated: PP=%.2f "
                "R1=%.2f R2=%.2f R3=%.2f "
                "S1=%.2f S2=%.2f S3=%.2f",
                ticker, pp, r1, r2, r3, s1, s2, s3
            )
            return levels

        except Exception as exc:
            logger.error(
                "[%s] Pivot calculation failed: %s",
                ticker, exc, exc_info=True
            )
            return None


# ── Level Monitor ──────────────────────────────────────────────────────────────

class LevelMonitor:
    """
    Monitors current price against calculated S/R levels.
    Generates LevelAlert when price approaches or breaks a level.

    Proximity rule : alert if price within PROXIMITY_PCT% of level
    Breakout rule  : alert if price has crossed to other side of level
    Cooldown       : 15 minutes per level to prevent spam
    """

    PROXIMITY_PCT = 0.3     # Alert when within 0.3% of a level
    COOLDOWN_MIN  = 15      # Minutes between alerts for same level

    def __init__(self) -> None:
        # Track last alert time per (ticker, level_name) to enforce cooldown
        self._last_alert: dict[tuple[str, str], datetime] = {}
        # Track last known side (above/below) per (ticker, level_name)
        self._last_side:  dict[tuple[str, str], str] = {}

    def check(
        self,
        current_price: float,
        levels: PivotLevels,
    ) -> list[LevelAlert]:
        """
        Check current price against all S/R levels.
        Returns list of LevelAlert objects (usually 0 or 1).
        """
        alerts: list[LevelAlert] = []
        now = datetime.now(timezone.utc)

        for level_name, (level_price, level_type) in levels.all_levels().items():

            key = (levels.ticker, level_name)

            # Skip pivot point for alerts (it's a reference, not S/R)
            if level_type == LevelType.PIVOT:
                continue

            # Distance from current price to this level
            distance_pct = ((current_price - level_price) / level_price) * 100
            abs_dist     = abs(distance_pct)

            # Determine current side (above or below this level)
            current_side = "above" if current_price > level_price else "below"
            last_side    = self._last_side.get(key)

            # ── Detect breakout/breakdown ──────────────────────────────────
            if last_side is not None and last_side != current_side:
                # Price has crossed this level since last check
                if level_type == LevelType.RESISTANCE and current_side == "above":
                    alert_type = LevelAlertType.BREAKOUT
                elif level_type == LevelType.SUPPORT and current_side == "below":
                    alert_type = LevelAlertType.BREAKDOWN
                else:
                    alert_type = None

                if alert_type and not self._in_cooldown(key, now):
                    alerts.append(LevelAlert(
                        ticker=levels.ticker,
                        display_name=levels.display_name,
                        alert_type=alert_type,
                        level_name=level_name,
                        level_type=level_type,
                        level_price=level_price,
                        current_price=current_price,
                        distance_pct=distance_pct,
                        levels=levels,
                        generated_at=now,
                    ))
                    self._last_alert[key] = now
                    logger.info(
                        "[%s] %s: %s at %s (%.2f)",
                        levels.ticker, alert_type.value,
                        level_name, current_price, distance_pct
                    )

            # ── Detect approaching ─────────────────────────────────────────
            elif abs_dist <= self.PROXIMITY_PCT:
                if not self._in_cooldown(key, now):
                    alerts.append(LevelAlert(
                        ticker=levels.ticker,
                        display_name=levels.display_name,
                        alert_type=LevelAlertType.APPROACHING,
                        level_name=level_name,
                        level_type=level_type,
                        level_price=level_price,
                        current_price=current_price,
                        distance_pct=distance_pct,
                        levels=levels,
                        generated_at=now,
                    ))
                    self._last_alert[key] = now
                    logger.info(
                        "[%s] APPROACHING %s (%s) — %.3f%% away",
                        levels.ticker, level_name, level_type.value, abs_dist
                    )

            # Update tracked side for next cycle
            self._last_side[key] = current_side

        return alerts

    def _in_cooldown(
        self,
        key: tuple[str, str],
        now: datetime,
    ) -> bool:
        """Returns True if this level was alerted within cooldown window."""
        last = self._last_alert.get(key)
        if last is None:
            return False
        from datetime import timedelta
        return (now - last).total_seconds() < self.COOLDOWN_MIN * 60