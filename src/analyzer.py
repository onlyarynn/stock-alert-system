"""
analyzer.py — Signal analysis layer.
Detects NORMAL and CRITICAL price movements and generates AlertSignals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from .config import TICKER_DISPLAY_NAMES, get_settings
from .database import AlertRepository, PriceRepository, get_session
from .fetcher import PriceData

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────────────────

class AlertLevel(str, Enum):
    """
    NORMAL   — change >= ALERT_THRESHOLD_PCT (e.g. 0.5%)
               Standard cooldown applies (30 min).
    CRITICAL — change >= CRITICAL_THRESHOLD_PCT (e.g. 1.5%)
               Bypasses normal cooldown. Repeats every 5 min.
               Requires immediate buy/sell/hold decision.
    """
    NORMAL   = "NORMAL"
    CRITICAL = "CRITICAL"


class Direction(str, Enum):
    RISING  = "RISING"
    FALLING = "FALLING"
    FLAT    = "FLAT"


# ── Alert Signal ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AlertSignal:
    """
    Immutable object representing one confirmed alert event.
    Created by MarketAnalyzer.analyze(), passed to EmailNotifier.send().
    """
    ticker:         str
    display_name:   str
    direction:      Direction
    change_pct:     float
    current_price:  float
    previous_price: float
    price_source:   str
    generated_at:   datetime
    level:          AlertLevel = AlertLevel.NORMAL

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def level_badge(self) -> str:
        return "🚨 CRITICAL" if self.level == AlertLevel.CRITICAL else "🔔 NORMAL"

    @property
    def direction_arrow(self) -> str:
        return "▲" if self.direction == Direction.RISING else "▼"

    @property
    def abs_change_pct(self) -> float:
        return abs(self.change_pct)

    @property
    def ist_time_str(self) -> str:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        ist_time = self.generated_at.astimezone(ist)
        return ist_time.strftime("%d %b %Y, %I:%M %p IST")

    # ── Email formatting ───────────────────────────────────────────────────────

    def format_email_subject(self) -> str:
        sign   = "+" if self.change_pct > 0 else ""
        prefix = "[🚨 CRITICAL ALERT]" if self.level == AlertLevel.CRITICAL \
                 else "[ALERT]"
        return (
            f"{prefix} {self.display_name} "
            f"{self.direction_arrow} {self.direction.value} "
            f"{sign}{self.change_pct:.2f}% | "
            f"{self.current_price:,.2f}"
        )

    def format_email_body(self) -> str:
        sign      = "+" if self.change_pct > 0 else ""
        separator = "=" * 45
        settings  = get_settings()

        if self.level == AlertLevel.CRITICAL:
            urgency_line1 = "⚠️  URGENT: Major market movement detected!"
            urgency_line2 = "Act now — review your positions immediately."
        else:
            urgency_line1 = "Standard market movement alert."
            urgency_line2 = "Monitor the situation."

        return f"""
{separator}
  {self.level_badge} — {self.display_name}
{separator}

  Index         : {self.display_name}
  Alert Level   : {self.level_badge}
  Direction     : {self.direction_arrow} {self.direction.value}
  Change        : {sign}{self.change_pct:.2f}%
  Current Price : {self.current_price:,.2f}
  Previous Price: {self.previous_price:,.2f}
  Price Move    : {self.current_price - self.previous_price:+,.2f} points
  Data Source   : {self.price_source}
  Alert Time    : {self.ist_time_str}

{separator}
{urgency_line1}
{urgency_line2}

Thresholds — Normal: >={settings.ALERT_THRESHOLD_PCT}% | Critical: >={settings.CRITICAL_THRESHOLD_PCT}%
This is an automated alert from your Stock Alert System.
{separator}
        """.strip()


# ── Market Analyzer ────────────────────────────────────────────────────────────

class MarketAnalyzer:
    """
    Core decision engine.
    Compares prices, applies threshold + cooldown rules,
    returns AlertSignal (NORMAL or CRITICAL) or None.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    def analyze(self, price_data: PriceData) -> Optional[AlertSignal]:
        """
        Analyse one fresh price and decide if an alert is needed.
        Returns AlertSignal or None.
        """
        ticker  = price_data.ticker
        current = price_data.price

        with get_session() as session:

            # ── Step 1: Load previous price ────────────────────────────────
            previous = PriceRepository.get_last_price(session, ticker)

            if previous is None:
                logger.info(
                    "[%s] First run — storing baseline price %.2f",
                    ticker, current
                )
                PriceRepository.upsert_price(session, ticker, current)
                return None

            # ── Step 2: Calculate % change ─────────────────────────────────
            change_pct = ((current - previous) / previous) * 100

            if change_pct > 0:
                direction = Direction.RISING
            elif change_pct < 0:
                direction = Direction.FALLING
            else:
                direction = Direction.FLAT

            logger.debug(
                "[%s] Current: %.2f | Previous: %.2f | Change: %+.3f%% (%s)",
                ticker, current, previous, change_pct, direction.value
            )

            # Always update stored price for next cycle
            PriceRepository.upsert_price(session, ticker, current)

            # ── Step 3: Check threshold + classify level ───────────────────
            if direction == Direction.FLAT:
                return None

            if abs(change_pct) < self._settings.ALERT_THRESHOLD_PCT:
                logger.debug(
                    "[%s] Change %.3f%% below threshold %.3f%% — no alert",
                    ticker, abs(change_pct), self._settings.ALERT_THRESHOLD_PCT
                )
                return None

            # Classify as CRITICAL or NORMAL
            if abs(change_pct) >= self._settings.CRITICAL_THRESHOLD_PCT:
                alert_level = AlertLevel.CRITICAL
            else:
                alert_level = AlertLevel.NORMAL

            # ── Step 4: Apply cooldown rules per level ─────────────────────
            if alert_level == AlertLevel.CRITICAL:
                if self._is_in_critical_cooldown(session, ticker):
                    logger.info(
                        "[%s] CRITICAL threshold crossed but "
                        "critical cooldown active — suppressed",
                        ticker
                    )
                    return None
                logger.warning(
                    "[%s] 🚨 CRITICAL movement: %+.2f%%",
                    ticker, change_pct
                )
            else:
                if self._is_in_cooldown(session, ticker):
                    logger.info(
                        "[%s] Normal threshold crossed but "
                        "cooldown active — suppressed",
                        ticker
                    )
                    return None

        # ── Step 5: Build and return signal ────────────────────────────────
        signal = AlertSignal(
            ticker=ticker,
            display_name=TICKER_DISPLAY_NAMES.get(ticker, ticker),
            direction=direction,
            change_pct=round(change_pct, 4),
            current_price=current,
            previous_price=previous,
            price_source=price_data.source,
            generated_at=datetime.now(timezone.utc),
            level=alert_level,
        )

        logger.info(
            "Alert signal generated: %s %s %s %+.2f%%",
            signal.level_badge,
            signal.display_name,
            signal.direction.value,
            signal.change_pct,
        )
        return signal

    # ── Cooldown helpers ───────────────────────────────────────────────────────

    def _is_in_cooldown(self, session, ticker: str) -> bool:
        """Standard 30-min cooldown for NORMAL alerts."""
        last_sent = AlertRepository.get_last_sent_time(session, ticker)
        if last_sent is None:
            return False
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        elapsed  = datetime.now(timezone.utc) - last_sent
        cooldown = timedelta(minutes=self._settings.COOLDOWN_MINUTES)
        in_cooldown = elapsed < cooldown
        if in_cooldown:
            remaining = int((cooldown - elapsed).total_seconds() / 60)
            logger.debug("[%s] Cooldown: %d min remaining", ticker, remaining)
        return in_cooldown

    def _is_in_critical_cooldown(self, session, ticker: str) -> bool:
        """Short 5-min cooldown for CRITICAL alerts only."""
        last_sent = AlertRepository.get_last_critical_alert_time(
            session, ticker
        )
        if last_sent is None:
            return False
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        elapsed  = datetime.now(timezone.utc) - last_sent
        cooldown = timedelta(minutes=self._settings.CRITICAL_COOLDOWN_MINUTES)
        in_cooldown = elapsed < cooldown
        if in_cooldown:
            remaining = int((cooldown - elapsed).total_seconds() / 60)
            logger.debug(
                "[%s] Critical cooldown: %d min remaining", ticker, remaining
            )
        return in_cooldown