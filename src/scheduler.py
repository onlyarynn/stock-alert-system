"""
scheduler.py
------------
Job scheduling layer — orchestrates the full monitoring cycle.
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
from .calendar import MarketCalendar
from .config import get_settings
from .fetcher import MarketDataFetcher
from .levels import LevelMonitor, PivotCalculator
from .notifier import EmailNotifier
from .briefing import BriefingService


logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class StockAlertScheduler:

    def __init__(self) -> None:
        self._settings      = get_settings()
        self._fetcher       = MarketDataFetcher(provider=__import__(
            'src.fetcher', fromlist=['YFinanceProvider']
        ).YFinanceProvider())
        self._analyzer      = MarketAnalyzer()
        self._notifier      = EmailNotifier()
        self._scheduler     = BlockingScheduler(timezone=str(IST))
        self._cycle         = 0
        self._briefing = BriefingService()
        self._calendar      = MarketCalendar()
        self._pivot_calc    = PivotCalculator()
        self._level_monitor = LevelMonitor()
        self._pivot_levels: dict[str, object] = {}

        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT,  self._handle_shutdown)

    def start(self) -> None:
        interval = self._settings.POLL_INTERVAL_SECONDS
        self._scheduler.add_job(
            func=self._run_cycle,
            trigger=IntervalTrigger(seconds=interval),
            id="market_poll",
            name="Market price poll",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
 # ── Morning briefing job — 8:45 AM IST every weekday ──────────────
        from apscheduler.triggers.cron import CronTrigger
        self._scheduler.add_job(
            func=self._briefing.send_morning_briefing,
            trigger=CronTrigger(
                hour=8, minute=45,
                day_of_week="mon-fri",
                timezone=str(IST),
            ),
            id="morning_briefing",
            name="Pre-market morning briefing",
            max_instances=1,
            coalesce=True,
        )

        # ── EOD summary job — 3:35 PM IST every weekday ───────────────────
        self._scheduler.add_job(
            func=self._briefing.send_eod_summary,
            trigger=CronTrigger(
                hour=15, minute=35,
                day_of_week="mon-fri",
                timezone=str(IST),
            ),
            id="eod_summary",
            name="End of day summary",
            max_instances=1,
            coalesce=True,
        )

        logger.info("  Briefing jobs : 8:45 AM (morning) | 3:35 PM (EOD)")


    # ── Core Cycle ─────────────────────────────────────────────────────────────

    def _run_cycle(self) -> None:
        self._cycle += 1
        now_ist   = datetime.now(IST)
        today_ist = now_ist.date()

        # ── Holiday / Weekend check ────────────────────────────────────────
        if not self._calendar.is_trading_day(today_ist):
            reason       = self._calendar.get_closure_reason(today_ist)
            next_day     = self._calendar.get_next_trading_day(today_ist)
            next_day_str = self._calendar.format_date(next_day)
            logger.info(
                "Cycle #%d | %s | Market CLOSED — %s | Resumes: %s",
                self._cycle,
                now_ist.strftime("%a %d %b"),
                reason,
                next_day_str,
            )
            self._notify_market_closed_once(reason, next_day_str)
            return

        # ── Market hours check ─────────────────────────────────────────────
        if not self._is_market_open(now_ist):
            logger.info(
                "Cycle #%d | %s IST | Market CLOSED — outside hours",
                self._cycle,
                now_ist.strftime("%H:%M"),
            )
            return

        logger.info(
            "---- Cycle #%d | %s IST ----",
            self._cycle,
            now_ist.strftime("%H:%M:%S"),
        )

        # ── Calculate pivot levels once per trading day ────────────────────
        for ticker in self._settings.watchlist_tickers:
            if ticker not in self._pivot_levels:
                lvl = self._pivot_calc.calculate(ticker)
                if lvl:
                    self._pivot_levels[ticker] = lvl
                    logger.info(
                        "[%s] Pivot levels: PP=%.2f R1=%.2f R2=%.2f "
                        "S1=%.2f S2=%.2f",
                        ticker,
                        lvl.pivot, lvl.r1, lvl.r2,
                        lvl.s1, lvl.s2,
                    )
                else:
                    logger.warning(
                        "[%s] Pivot calculation failed — "
                        "S/R alerts disabled this cycle",
                        ticker,
                    )

        # ── Fetch all prices ───────────────────────────────────────────────
        tickers        = self._settings.watchlist_tickers
        price_map      = self._fetcher.fetch_all(tickers)
        alerts_sent    = 0
        alerts_skipped = 0
        fetch_errors   = 0

        for ticker, price_data in price_map.items():

            if price_data is None:
                logger.warning("[%s] No price data — skipping", ticker)
                fetch_errors += 1
                continue

            try:
                # ── Normal % change alert ──────────────────────────────────
                sig = self._analyzer.analyze(price_data)
                if sig is None:
                    alerts_skipped += 1
                else:
                    result = self._notifier.send(sig)
                    if result.success:
                        alerts_sent += 1
                        logger.info(
                            "[%s] Alert sent | %s %+.2f%%",
                            ticker, sig.direction.value, sig.change_pct,
                        )
                    else:
                        logger.error(
                            "[%s] Alert FAILED: %s",
                            ticker, result.error_message,
                        )

                # ── Support / Resistance level alerts ──────────────────────
                pivot_levels = self._pivot_levels.get(ticker)
                if pivot_levels:
                    level_alerts = self._level_monitor.check(
                        current_price=price_data.price,
                        levels=pivot_levels,
                    )
                    for level_alert in level_alerts:
                        lvl_result = self._notifier.send_level_alert(
                            level_alert
                        )
                        if lvl_result.success:
                            alerts_sent += 1
                            logger.info(
                                "[%s] Level alert sent | %s %s",
                                ticker,
                                level_alert.alert_type.value,
                                level_alert.level_name,
                            )
                        else:
                            logger.error(
                                "[%s] Level alert FAILED: %s",
                                ticker, lvl_result.error_message,
                            )

            except Exception as exc:
                logger.error(
                    "[%s] Unexpected error: %s", ticker, exc,
                    exc_info=True,
                )

        logger.info(
            "---- Cycle #%d complete | Sent: %d | Skipped: %d | Errors: %d ----",
            self._cycle, alerts_sent, alerts_skipped, fetch_errors,
        )

    # ── Market closed notification ─────────────────────────────────────────────

    def _notify_market_closed_once(
        self,
        reason: str,
        next_trading_day_str: str,
    ) -> None:
        """Sends market-closed email ONCE per closed day."""
        from .database import AlertRepository, get_session
        try:
            with get_session() as session:
                if AlertRepository.was_closure_notified_today(session):
                    logger.debug(
                        "Market closed notification already sent today"
                    )
                    return
                upcoming = self._calendar.get_upcoming_holidays(days_ahead=30)
                result   = self._notifier.send_market_closed_email(
                    reason=reason,
                    next_trading_day=next_trading_day_str,
                    upcoming_holidays=upcoming,
                )
                if result.success:
                    AlertRepository.record_closure_notification(
                        session, reason
                    )
                    logger.info("Market closed notification sent.")
                else:
                    logger.error(
                        "Failed to send market closed notification: %s",
                        result.error_message
                    )
        except Exception as exc:
            logger.error(
                "Error in _notify_market_closed_once: %s", exc,
                exc_info=True
            )

    # ── Market Hours ───────────────────────────────────────────────────────────

    def _is_market_open(self, now: datetime) -> bool:
        s = self._settings
        if now.weekday() >= 5:
            return False
        if now.hour < s.MARKET_OPEN_HOUR:
            return False
        if now.hour == s.MARKET_OPEN_HOUR and now.minute < s.MARKET_OPEN_MINUTE:
            return False
        if now.hour > s.MARKET_CLOSE_HOUR:
            return False
        if now.hour == s.MARKET_CLOSE_HOUR and now.minute > s.MARKET_CLOSE_MINUTE:
            return False
        return True