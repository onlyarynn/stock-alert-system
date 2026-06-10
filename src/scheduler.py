"""
scheduler.py
------------
Job scheduling layer — orchestrates the full monitoring cycle.

Wires together fetcher → analyzer → notifier and drives
the recurring poll cycle using APScheduler.

Features:
  - Market hours enforcement (IST 9:15am–3:30pm, Mon–Fri only)
  - Per-ticker error isolation (one ticker failing won't stop others)
  - Graceful shutdown on Ctrl+C or SIGTERM
  - Detailed cycle logging (every fetch, every decision)
  - Immediate first cycle on startup (no waiting for first interval)
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .analyzer import MarketAnalyzer
from .config import get_settings
from .fetcher import MarketDataFetcher
from .notifier import EmailNotifier

logger = logging.getLogger(__name__)

# India Standard Time zone
IST = ZoneInfo("Asia/Kolkata")


class StockAlertScheduler:
    """
    Orchestrates the full monitoring pipeline.

    Creates one instance of each component at startup and
    reuses them across all cycles — avoids repeated initialisation
    overhead and keeps database connections efficient.

    Usage (from main.py):
        scheduler = StockAlertScheduler()
        scheduler.start()   # blocks until shutdown
    """

    def __init__(self) -> None:
        self._settings  = get_settings()
        self._fetcher   = MarketDataFetcher(provider=__import__(
            'src.fetcher', fromlist=['YFinanceProvider']
        ).YFinanceProvider())
        self._analyzer  = MarketAnalyzer()
        self._notifier  = EmailNotifier()
        self._scheduler = BlockingScheduler(timezone=str(IST))
        self._cycle     = 0

        # Register shutdown handlers
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT,  self._handle_shutdown)

    def start(self) -> None:
        """
        Add the polling job to APScheduler and start the loop.
        Runs an immediate first cycle before the first interval.
        Blocks until shutdown signal is received.
        """
        interval = self._settings.POLL_INTERVAL_SECONDS

        self._scheduler.add_job(
            func=self._run_cycle,
            trigger=IntervalTrigger(seconds=interval),
            id="market_poll",
            name="Market price poll",
            max_instances=1,      # never run two cycles at once
            coalesce=True,        # skip missed runs, don't stack up
            misfire_grace_time=60,
        )

        logger.info("=" * 55)
        logger.info("  Scheduler started")
        logger.info("  Polling every : %d seconds (%d min)",
                    interval, interval // 60)
        logger.info("  Watchlist     : %s",
                    self._settings.watchlist_tickers)
        logger.info("  Threshold     : %.2f%%",
                    self._settings.ALERT_THRESHOLD_PCT)
        logger.info("  Cooldown      : %d min",
                    self._settings.COOLDOWN_MINUTES)
        logger.info("  Market hours  : %02d:%02d – %02d:%02d IST (Mon–Fri)",
                    self._settings.MARKET_OPEN_HOUR,
                    self._settings.MARKET_OPEN_MINUTE,
                    self._settings.MARKET_CLOSE_HOUR,
                    self._settings.MARKET_CLOSE_MINUTE)
        logger.info("=" * 55)

        # Run once immediately so we don't wait for the first interval
        self._run_cycle()

        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped.")

    def _handle_shutdown(self, signum, frame) -> None:
        """Handle SIGTERM / SIGINT — shut down cleanly."""
        logger.info(
            "Shutdown signal received (%s) — stopping scheduler…",
            signum
        )
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        sys.exit(0)

    # ── Core Cycle ─────────────────────────────────────────────────────────────

    def _run_cycle(self) -> None:
        """
        One complete monitoring cycle:
          1. Skip if outside market hours
          2. Fetch all ticker prices
          3. Analyze each price
          4. Send alert if signal generated

        Errors on individual tickers are caught and logged —
        one failure never stops the others from being processed.
        """
        self._cycle += 1
        now_ist = datetime.now(IST)

        # ── Market hours check ─────────────────────────────────────────────
        if not self._is_market_open(now_ist):
            logger.info(
                "Cycle #%d | %s IST | Market CLOSED — skipping",
                self._cycle,
                now_ist.strftime("%a %d %b, %H:%M")
            )
            return

        logger.info(
            "──── Cycle #%d | %s IST ────",
            self._cycle,
            now_ist.strftime("%H:%M:%S")
        )

        # ── Fetch all prices ───────────────────────────────────────────────
        tickers   = self._settings.watchlist_tickers
        price_map = self._fetcher.fetch_all(tickers)

        alerts_sent  = 0
        alerts_skipped = 0
        fetch_errors = 0

        # ── Analyze and notify ─────────────────────────────────────────────
        for ticker, price_data in price_map.items():

            if price_data is None:
                logger.warning(
                    "[%s] No price data returned — skipping analysis",
                    ticker
                )
                fetch_errors += 1
                continue

            try:
                signal = self._analyzer.analyze(price_data)

                if signal is None:
                    logger.debug(
                        "[%s] No alert signal — price change within normal range",
                        ticker
                    )
                    alerts_skipped += 1
                    continue

                # Signal generated — send the email
                result = self._notifier.send(signal)

                if result.success:
                    alerts_sent += 1
                    logger.info(
                        "[%s] Alert email sent ✓ | %s %+.2f%%",
                        ticker,
                        signal.direction.value,
                        signal.change_pct,
                    )
                else:
                    logger.error(
                        "[%s] Alert email FAILED: %s",
                        ticker,
                        result.error_message,
                    )

            except Exception as exc:
                # Isolate per-ticker errors — never let one crash the loop
                logger.error(
                    "[%s] Unexpected error in cycle: %s",
                    ticker, exc,
                    exc_info=True,
                )

        # ── Cycle summary ──────────────────────────────────────────────────
        logger.info(
            "──── Cycle #%d complete | Sent: %d | Skipped: %d | Errors: %d ────",
            self._cycle,
            alerts_sent,
            alerts_skipped,
            fetch_errors,
        )

    # ── Market Hours Check ─────────────────────────────────────────────────────

    def _is_market_open(self, now: datetime) -> bool:
        """
        Returns True only during NSE/BSE trading hours.

        Trading days  : Monday to Friday (weekday 0–4)
        Trading hours : 09:15 – 15:30 IST
        Excludes      : Weekends (Sat=5, Sun=6)

        Note: Indian public holidays are not filtered here.
        The system will attempt to fetch prices on holidays —
        yFinance will simply return the last available price,
        which will be unchanged and trigger no alert.
        """
        s = self._settings

        # Weekends
        if now.weekday() >= 5:
            return False

        # Before market open
        if now.hour < s.MARKET_OPEN_HOUR:
            return False
        if now.hour == s.MARKET_OPEN_HOUR and now.minute < s.MARKET_OPEN_MINUTE:
            return False

        # After market close
        if now.hour > s.MARKET_CLOSE_HOUR:
            return False
        if now.hour == s.MARKET_CLOSE_HOUR and now.minute > s.MARKET_CLOSE_MINUTE:
            return False

        return True