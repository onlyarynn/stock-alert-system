# ── Official NSE/BSE Holiday List 2026 

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Source : NSE Circular NSE/CMTR/71775 dated December 12, 2025

HOLIDAYS_2026 = [
    (2026,  1, 15, "Maharashtra Municipal Corporation Election"),
    (2026,  1, 26, "Republic Day"),
    (2026,  3,  3, "Holi"),
    (2026,  3, 26, "Shri Ram Navami"),
    (2026,  3, 31, "Shri Mahavir Jayanti"),
    (2026,  4,  3, "Good Friday"),
    (2026,  4, 14, "Dr. Baba Saheb Ambedkar Jayanti"),
    (2026,  5,  1, "Maharashtra Day"),
    (2026,  5, 28, "Bakri Id (Eid ul-Adha)"),
    (2026,  6, 26, "Muharram"),
    (2026,  9, 14, "Ganesh Chaturthi"),
    (2026, 10,  2, "Mahatma Gandhi Jayanti"),
    (2026, 10, 20, "Dussehra"),
    (2026, 11, 10, "Diwali Balipratipada"),
    (2026, 11, 25, "Prakash Gurpurb Sri Guru Nanak Dev Ji"),
    (2026, 12, 25, "Christmas"),
]

WEEKEND_HOLIDAYS_2026_INFO = [
    (2026,  8, 15, "Independence Day (Saturday — no additional closure)"),
    (2026, 11,  8, "Diwali Laxmi Pujan (Sunday — Muhurat Trading evening only)"),
]

# Map year → weekday holiday list
ALL_HOLIDAYS: dict[int, list] = {
    2026: HOLIDAYS_2026,
}

class MarketCalendar:
    def __init__(self) -> None:
        self._holiday_map: dict[date, str] = {}
        self._load_holidays()

    def _load_holidays(self) -> None:
        """Build a fast date → holiday name lookup dictionary."""
        for year, holiday_list in ALL_HOLIDAYS.items():
            for y, m, d, name in holiday_list:
                self._holiday_map[date(y, m, d)] = name
        logger.debug(
            "Holiday calendar loaded: %d holidays across %d year(s)",
            len(self._holiday_map),
            len(ALL_HOLIDAYS),
        )

    def is_trading_day(self, check_date: date | None = None) -> bool:
        """
        Returns True if NSE/BSE is open for trading on check_date.
        Defaults to today (IST) if no date provided.
        """
        if check_date is None:
            check_date = datetime.now(IST).date()

        # Check 1 — Weekend
        if check_date.weekday() >= 5:
            return False

        # Check 2 — Unknown year (warn and assume open)
        if check_date.year not in ALL_HOLIDAYS:
            logger.warning(
                "No holiday data for year %d. "
                "Please update src/calendar.py with the official "
                "NSE circular for %d (published every November).",
                check_date.year,
                check_date.year,
            )
            return True

        # Check 3 — Public holiday
        return check_date not in self._holiday_map

    def get_closure_reason(self, check_date: date | None = None) -> str:
        """
        Returns a human-readable reason the market is closed.
        Returns empty string if market is open.
        """
        if check_date is None:
            check_date = datetime.now(IST).date()

        if check_date.weekday() == 5:
            return "Saturday — weekly off"
        if check_date.weekday() == 6:
            return "Sunday — weekly off"
        if check_date in self._holiday_map:
            return f"Public Holiday — {self._holiday_map[check_date]}"
        return ""

    def get_next_trading_day(self, from_date: date | None = None) -> date:
        """
        Returns the next trading day after from_date.
        Skips over weekends and holidays automatically.
        """
        if from_date is None:
            from_date = datetime.now(IST).date()
        next_day = from_date + timedelta(days=1)
        while not self.is_trading_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    def get_upcoming_holidays(self, days_ahead: int = 30) -> list[tuple[date, str]]:
        """
        Returns list of (date, name) for upcoming NSE holidays
        within the next days_ahead days from today (IST).
        """
        today    = datetime.now(IST).date()
        end_date = today + timedelta(days=days_ahead)
        upcoming = []
        check    = today
        while check <= end_date:
            if check in self._holiday_map:
                upcoming.append((check, self._holiday_map[check]))
            check += timedelta(days=1)
        return upcoming

    def format_date(self, d: date) -> str:
        """Returns readable date string — e.g. 'Fri, 26 Jun 2026'."""
        return d.strftime("%a, %d %b %Y")