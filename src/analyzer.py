
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from .config import TICKER_DISPLAY_NAMES, get_settings
from .database import AlertRepository, PriceRepository, get_session
from .fetcher import PriceData

logger = logging.getLogger(__name__)

# ── Direction Enum 

class Direction(str, Enum):
    """
    Represents the direction of a price movement.
    Inheriting from str means Direction.RISING == "RISING" is True,
    which makes database storage and logging straightforward.
    """
    RISING  = "RISING"
    FALLING = "FALLING"
    FLAT    = "FLAT"


# ── Alert Signal

@dataclass(frozen=True)
class AlertSignal:
    """
    Immutable object representing a confirmed alert event.

    Created by MarketAnalyzer.analyze() when all conditions are met.
    Passed to AlertNotifier.send() which formats and dispatches it.
    Stored in the database by AlertNotifier after sending.

    frozen=True — values cannot be changed after creation.
    """
    ticker:         str
    display_name:   str
    direction:      Direction
    change_pct:     float
    current_price:  float
    previous_price: float
    price_source:   str
    generated_at:   datetime

    # ── Computed helpers

    @property
    def direction_arrow(self) -> str:
        """Returns ▲ for rising, ▼ for falling."""
        return "▲" if self.direction == Direction.RISING else "▼"

    @property
    def abs_change_pct(self) -> float:
        """Absolute value of the percentage change."""
        return abs(self.change_pct)

    @property
    def ist_time_str(self) -> str:
        """Formatted IST timestamp string for display in emails."""
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        ist_time = self.generated_at.astimezone(ist)
        return ist_time.strftime("%d %b %Y, %I:%M %p IST")

    # ── Email formatting

    def format_email_subject(self) -> str:
        """
        Produces a clear, scannable email subject line.
        Example: [ALERT] Nifty 50 (NSE) ▲ RISING +1.25% | 24,850.00
        """
        sign = "+" if self.change_pct > 0 else ""
        return (
            f"[ALERT] {self.display_name} "
            f"{self.direction_arrow} {self.direction.value} "
            f"{sign}{self.change_pct:.2f}% | "
            f"{self.current_price:,.2f}"
        )

    def format_email_body(self) -> str:
        """
        Produces a well-structured plain-text email body.
        Contains all details needed to make a trading decision.
        """
        sign      = "+" if self.change_pct > 0 else ""
        separator = "=" * 45

        return f"""
{separator}
  STOCK MARKET ALERT — {self.display_name}
{separator}

  Index         : {self.display_name}
  Direction     : {self.direction_arrow} {self.direction.value}
  Change        : {sign}{self.change_pct:.2f}%
  Current Price : {self.current_price:,.2f}
  Previous Price: {self.previous_price:,.2f}
  Price Move    : {self.current_price - self.previous_price:+,.2f} points
  Data Source   : {self.price_source}
  Alert Time    : {self.ist_time_str}

{separator}

This is an automated alert from your Stock Alert System.
Threshold set at {get_settings().ALERT_THRESHOLD_PCT}% price change.

{separator}
        """.strip()


# ── Market Analyzer 

class MarketAnalyzer:
    """
    Core decision engine — decides whether to fire an alert.

    For each incoming PriceData object, analyze() goes through:
      1. Load previous price from database
      2. Calculate percentage change
      3. Skip if change is below threshold
      4. Skip if cooldown period has not expired
      5. Return AlertSignal if all checks pass, else None

    Each call opens and closes its own database session, making
    this class safe to call repeatedly from the scheduler loop.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    def analyze(self, price_data: PriceData) -> Optional[AlertSignal]:
        """
        Analyse one fresh price reading and decide if an alert is needed.

        Returns:
            AlertSignal  — if an alert should be sent
            None         — if no alert is needed
        """
        ticker  = price_data.ticker
        current = price_data.price

        with get_session() as session:

            # ── Step 1: Load previous price ────────────────────────────────
            previous = PriceRepository.get_last_price(session, ticker)

            if previous is None:
                # First run — no baseline yet. Store price and wait.
                logger.info(
                    "[%s] First run — storing baseline price %.2f",
                    ticker, current
                )
                PriceRepository.upsert_price(session, ticker, current)
                return None

            # ── Step 2: Calculate percentage change ────────────────────────
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

            # Always update the stored price for the next cycle
            PriceRepository.upsert_price(session, ticker, current)

            # ── Step 3: Check threshold ────────────────────────────────────
            if abs(change_pct) < self._settings.ALERT_THRESHOLD_PCT:
                logger.debug(
                    "[%s] Change %.3f%% is below threshold %.3f%% — no alert",
                    ticker, abs(change_pct), self._settings.ALERT_THRESHOLD_PCT
                )
                return None

            if direction == Direction.FLAT:
                return None

            # ── Step 4: Check cooldown ─────────────────────────────────────
            if self._is_in_cooldown(session, ticker):
                logger.info(
                    "[%s] Threshold crossed but cooldown active — alert suppressed",
                    ticker
                )
                return None

        # ── Step 5: Build and return the alert signal ──────────────────────
        signal = AlertSignal(
            ticker=ticker,
            display_name=TICKER_DISPLAY_NAMES.get(ticker, ticker),
            direction=direction,
            change_pct=round(change_pct, 4),
            current_price=current,
            previous_price=previous,
            price_source=price_data.source,
            generated_at=datetime.now(timezone.utc),
        )

        logger.info(
            "Alert signal generated: %s %s %+.2f%%",
            signal.display_name,
            signal.direction.value,
            signal.change_pct,
        )
        return signal

    # ── Private helpers

    def _is_in_cooldown(self, session, ticker: str) -> bool:
        """
        Returns True if an alert was already sent for this ticker
        within the configured cooldown window.

        Reads from the database so cooldown survives app restarts.
        """
        last_sent = AlertRepository.get_last_sent_time(session, ticker)

        if last_sent is None:
            return False

        # Ensure last_sent is timezone-aware for comparison
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)

        elapsed  = datetime.now(timezone.utc) - last_sent
        cooldown = timedelta(minutes=self._settings.COOLDOWN_MINUTES)

        in_cooldown = elapsed < cooldown
        if in_cooldown:
            remaining = int((cooldown - elapsed).total_seconds() / 60)
            logger.debug(
                "[%s] Cooldown active — %d min remaining",
                ticker, remaining
            )
        return in_cooldown