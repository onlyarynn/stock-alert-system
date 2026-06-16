"""
-----------
Pre-Market Morning Briefing (8:45 AM IST) and
End-of-Day Summary (3:35 PM IST).

"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from zoneinfo import ZoneInfo

import yfinance as yf

from .calendar import MarketCalendar
from .config import get_settings
from .database import AlertRepository, AlertRecord, get_session
from .telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


# ── Global Signal Tickers ──────────────────────────────────────────────────────

GLOBAL_TICKERS = {
    "^DJI":  "Dow Jones",
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq",
    "CL=F":  "Crude Oil (WTI)",
    "INR=X": "USD/INR",
}

INDIAN_TICKERS = {
    "^NSEI":  "Nifty 50",
    "^BSESN": "Sensex",
}


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class TickerSnapshot:
    """One ticker's price snapshot for briefing use."""
    ticker:     str
    name:       str
    price:      float
    prev_close: float
    change_pct: float
    unit:       str = ""

    @property
    def change_emoji(self) -> str:
        if self.change_pct > 0.5:
            return "🟢"
        elif self.change_pct < -0.5:
            return "🔴"
        return "🟡"

    @property
    def arrow(self) -> str:
        return "▲" if self.change_pct >= 0 else "▼"

    @property
    def sign(self) -> str:
        return "+" if self.change_pct >= 0 else ""


# ── Data Fetcher ───────────────────────────────────────────────────────────────

class BriefingFetcher:
    """
    Fetches all data needed for morning briefing and EOD summary.
    Uses yFinance — same library already in your project.
    """

    def fetch_snapshot(
        self, ticker: str, name: str, unit: str = ""
    ) -> Optional[TickerSnapshot]:
        """Fetch latest price + previous close for one ticker."""
        try:
            t    = yf.Ticker(ticker)
            info = t.fast_info

            price      = float(info.last_price or 0)
            prev_close = float(info.previous_close or 0)

            if price <= 0 or prev_close <= 0:
                hist = t.history(period="2d", interval="1d")
                if len(hist) >= 2:
                    price      = float(hist["Close"].iloc[-1])
                    prev_close = float(hist["Close"].iloc[-2])
                elif len(hist) == 1:
                    price      = float(hist["Close"].iloc[-1])
                    prev_close = price
                else:
                    logger.warning("No data for %s", ticker)
                    return None

            change_pct = ((price - prev_close) / prev_close) * 100

            return TickerSnapshot(
                ticker=ticker,
                name=name,
                price=round(price, 2),
                prev_close=round(prev_close, 2),
                change_pct=round(change_pct, 2),
                unit=unit,
            )

        except Exception as exc:
            logger.error("Failed to fetch %s: %s", ticker, exc)
            return None

    def fetch_all_global(self) -> list[TickerSnapshot]:
        """Fetch all global signal tickers."""
        results = []
        units = {
            "^DJI":  "",
            "^GSPC": "",
            "^IXIC": "",
            "CL=F":  "$",
            "INR=X": "₹",
        }
        for ticker, name in GLOBAL_TICKERS.items():
            snap = self.fetch_snapshot(ticker, name, units.get(ticker, ""))
            if snap:
                results.append(snap)
        return results

    def fetch_all_indian(self) -> list[TickerSnapshot]:
        """Fetch Nifty + Sensex."""
        results = []
        for ticker, name in INDIAN_TICKERS.items():
            snap = self.fetch_snapshot(ticker, name, "")
            if snap:
                results.append(snap)
        return results

    def get_alerts_yesterday(self) -> dict:
        """Pull yesterday's alert count from DB."""
        try:
            with get_session() as session:
                from sqlalchemy import func
                yesterday_start = datetime.now(IST).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - timedelta(days=1)
                yesterday_end = yesterday_start + timedelta(days=1)

                total = (
                    session.query(func.count(AlertRecord.id))
                    .filter(
                        AlertRecord.sent_at >= yesterday_start,
                        AlertRecord.sent_at <  yesterday_end,
                    )
                    .scalar()
                ) or 0

                critical = (
                    session.query(func.count(AlertRecord.id))
                    .filter(
                        AlertRecord.sent_at    >= yesterday_start,
                        AlertRecord.sent_at    <  yesterday_end,
                        AlertRecord.alert_level == "CRITICAL",
                    )
                    .scalar()
                ) or 0

                return {
                    "total":    total,
                    "critical": critical,
                    "normal":   total - critical,
                }

        except Exception as exc:
            logger.error("Failed to fetch yesterday's alerts: %s", exc)
            return {"total": 0, "critical": 0, "normal": 0}

    def get_alerts_today(self) -> list[dict]:
        """Pull today's alerts from DB for EOD summary."""
        try:
            with get_session() as session:
                today_start = datetime.now(IST).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                rows = (
                    session.query(AlertRecord)
                    .filter(AlertRecord.sent_at >= today_start)
                    .order_by(AlertRecord.sent_at)
                    .all()
                )
                alerts = []
                for row in rows:
                    ist_time = row.sent_at.astimezone(IST)
                    alerts.append({
                        "ticker":    row.ticker,
                        "name":      row.display_name,
                        "level":     row.alert_level,
                        "direction": row.direction,
                        "change":    row.change_pct,
                        "time":      ist_time.strftime("%I:%M %p"),
                    })
                return alerts

        except Exception as exc:
            logger.error("Failed to fetch today's alerts: %s", exc)
            return []


# ── Message Formatters ─────────────────────────────────────────────────────────

class BriefingFormatter:
    """
    Formats morning briefing and EOD summary for both
    Email (plain text) and Telegram (HTML).
    """

    # ── Morning Briefing ───────────────────────────────────────────────────────

    def format_morning_email(
        self,
        date_str: str,
        indian: list[TickerSnapshot],
        global_signals: list[TickerSnapshot],
        alerts_yesterday: dict,
        upcoming_holidays: list[tuple],
        tomorrow_status: str,
    ) -> tuple[str, str]:
        """Returns (subject, body) for morning briefing email."""

        subject = f"📊 Pre-Market Briefing — {date_str}"
        sep     = "=" * 50

        # ── Indian Indices ─────────────────────────────────────────────────
        indian_lines = []
        for s in indian:
            indian_lines.append(
                f"  {s.change_emoji} {s.name:<18} "
                f"{s.price:>10,.2f}  "
                f"({s.sign}{s.change_pct:.2f}% {s.arrow})"
            )

        # ── Global Signals ─────────────────────────────────────────────────
        global_lines = []
        for s in global_signals:
            if s.ticker == "INR=X":
                price_str = f"Rs.{s.price:.4f}"
            elif s.ticker == "CL=F":
                price_str = f"${s.price:.2f}/bbl"
            else:
                price_str = f"{s.price:,.2f}"

            warning = ""
            if s.ticker == "CL=F" and s.price > 100:
                warning = "  WARNING: HIGH OIL"
            elif s.ticker == "INR=X" and s.price > 85:
                warning = "  WARNING: WEAK RUPEE"
            elif s.change_pct < -1.5:
                warning = "  WARNING: WEAK"

            global_lines.append(
                f"  {s.change_emoji} {s.name:<18} "
                f"{price_str:>14}  "
                f"({s.sign}{s.change_pct:.2f}%){warning}"
            )

        # ── Holidays ───────────────────────────────────────────────────────
        # upcoming_holidays is list of (date, name) tuples from MarketCalendar
        if upcoming_holidays:
            today     = datetime.now(IST).date()
            hol_lines = []
            for h_date, h_name in upcoming_holidays[:3]:
                days_away = (h_date - today).days
                day_word  = "day" if days_away == 1 else "days"
                hol_lines.append(
                    f"  - {h_date.strftime('%d %b %Y')} — {h_name} "
                    f"({days_away} {day_word} away)"
                )
            hol_section = "\n".join(hol_lines)
        else:
            hol_section = "  No holidays in the next 14 days"

        # ── Yesterday's Alerts ─────────────────────────────────────────────
        if alerts_yesterday["total"] == 0:
            alert_line = "  No alerts yesterday"
        else:
            alert_line = (
                f"  {alerts_yesterday['total']} total  |  "
                f"{alerts_yesterday['critical']} CRITICAL  |  "
                f"{alerts_yesterday['normal']} NORMAL"
            )

        body = f"""
{sep}
  PRE-MARKET BRIEFING — {date_str}
{sep}

MARKET STATUS : OPEN TODAY
TOMORROW      : {tomorrow_status}

{sep}
  INDIAN INDICES (Previous Close)
{sep}
{chr(10).join(indian_lines)}

{sep}
  GLOBAL SIGNALS (Overnight)
{sep}
{chr(10).join(global_lines)}

{sep}
  UPCOMING HOLIDAYS
{sep}
{hol_section}

{sep}
  YESTERDAY'S ALERTS
{sep}
{alert_line}

{sep}
  Good luck today. Trade safe.
{sep}
        """.strip()

        return subject, body

    def format_morning_telegram(
        self,
        date_str: str,
        indian: list[TickerSnapshot],
        global_signals: list[TickerSnapshot],
        alerts_yesterday: dict,
        upcoming_holidays: list[tuple],
        tomorrow_status: str,
    ) -> str:
        """Formats morning briefing for Telegram (HTML)."""

        lines = [
            "📊 <b>Pre-Market Briefing</b>",
            f"<i>{date_str}</i>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "🇮🇳 <b>Indian Indices</b>",
        ]

        for s in indian:
            lines.append(
                f"{s.change_emoji} <b>{s.name}</b>: "
                f"<code>{s.price:,.2f}</code> "
                f"({s.sign}{s.change_pct:.2f}%)"
            )

        lines += ["", "🌍 <b>Global Signals</b>"]

        for s in global_signals:
            if s.ticker == "INR=X":
                price_str = f"Rs.{s.price:.4f}"
            elif s.ticker == "CL=F":
                price_str = f"${s.price:.2f}"
            else:
                price_str = f"{s.price:,.2f}"

            warning = ""
            if s.ticker == "CL=F" and s.price > 100:
                warning = " ⚠️"
            elif s.ticker == "INR=X" and s.price > 85:
                warning = " ⚠️"

            lines.append(
                f"{s.change_emoji} <b>{s.name}</b>: "
                f"<code>{price_str}</code> "
                f"({s.sign}{s.change_pct:.2f}%){warning}"
            )

        # Upcoming holidays — (date, name) tuples
        if upcoming_holidays:
            today = datetime.now(IST).date()
            lines += ["", "📅 <b>Upcoming Holidays</b>"]
            for h_date, h_name in upcoming_holidays[:2]:
                days_away = (h_date - today).days
                lines.append(
                    f"  🗓 {h_date.strftime('%d %b %Y')} — {h_name} "
                    f"({days_away}d)"
                )

        # Yesterday alerts
        lines += ["", "🔔 <b>Yesterday's Alerts</b>"]
        if alerts_yesterday["total"] == 0:
            lines.append("  No alerts yesterday")
        else:
            lines.append(
                f"  {alerts_yesterday['total']} total  |  "
                f"{alerts_yesterday['critical']} CRITICAL  |  "
                f"{alerts_yesterday['normal']} NORMAL"
            )

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Tomorrow: <b>{tomorrow_status}</b>",
            "<i>Trade safe today 📈</i>",
        ]

        return "\n".join(lines)

    # ── EOD Summary ────────────────────────────────────────────────────────────

    def format_eod_email(
        self,
        date_str: str,
        indian: list[TickerSnapshot],
        global_signals: list[TickerSnapshot],
        alerts_today: list[dict],
        tomorrow_status: str,
    ) -> tuple[str, str]:
        """Returns (subject, body) for EOD summary email."""

        subject = f"📈 EOD Summary — {date_str}"
        sep     = "=" * 50

        # ── Indian Indices ─────────────────────────────────────────────────
        indian_lines = []
        for s in indian:
            indian_lines.append(
                f"  {s.change_emoji} {s.name:<18} "
                f"{s.price:>10,.2f}  "
                f"({s.sign}{s.change_pct:.2f}% {s.arrow})"
            )

        # ── Global Signals ─────────────────────────────────────────────────
        global_lines = []
        for s in global_signals:
            if s.ticker == "INR=X":
                price_str = f"Rs.{s.price:.4f}"
            elif s.ticker == "CL=F":
                price_str = f"${s.price:.2f}/bbl"
            else:
                price_str = f"{s.price:,.2f}"
            global_lines.append(
                f"  {s.change_emoji} {s.name:<18} "
                f"{price_str:>14}  "
                f"({s.sign}{s.change_pct:.2f}%)"
            )

        # ── Today's Alerts ─────────────────────────────────────────────────
        total_alerts    = len(alerts_today)
        critical_alerts = [a for a in alerts_today if a["level"] == "CRITICAL"]

        if total_alerts == 0:
            alert_section = "  No alerts fired today — quiet session"
        else:
            alert_lines = [
                f"  Total: {total_alerts}  |  "
                f"CRITICAL: {len(critical_alerts)}  |  "
                f"NORMAL: {total_alerts - len(critical_alerts)}",
                "",
            ]
            for a in alerts_today[-5:]:
                direction_arrow = "▲" if a["direction"] == "RISING" else "▼"
                sign = "+" if a["change"] > 0 else ""
                alert_lines.append(
                    f"  {a['time']}  {a['level']:<8}  "
                    f"{a['name']:<20}  "
                    f"{direction_arrow} {sign}{a['change']:.2f}%"
                )
            alert_section = "\n".join(alert_lines)

        body = f"""
{sep}
  EOD SUMMARY — {date_str}
{sep}

{sep}
  INDIAN INDICES (Today's Close)
{sep}
{chr(10).join(indian_lines)}

{sep}
  GLOBAL SIGNALS
{sep}
{chr(10).join(global_lines)}

{sep}
  TODAY'S ALERTS
{sep}
{alert_section}

{sep}
  TOMORROW : {tomorrow_status}
{sep}
  Market closed for today. See you tomorrow.
{sep}
        """.strip()

        return subject, body

    def format_eod_telegram(
        self,
        date_str: str,
        indian: list[TickerSnapshot],
        global_signals: list[TickerSnapshot],
        alerts_today: list[dict],
        tomorrow_status: str,
    ) -> str:
        """Formats EOD summary for Telegram (HTML)."""

        total_alerts    = len(alerts_today)
        critical_alerts = [a for a in alerts_today if a["level"] == "CRITICAL"]

        lines = [
            "📈 <b>EOD Summary</b>",
            f"<i>{date_str}</i>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "🇮🇳 <b>Indian Indices</b>",
        ]

        for s in indian:
            lines.append(
                f"{s.change_emoji} <b>{s.name}</b>: "
                f"<code>{s.price:,.2f}</code> "
                f"({s.sign}{s.change_pct:.2f}%)"
            )

        lines += ["", "🌍 <b>Global Signals</b>"]
        for s in global_signals:
            if s.ticker == "INR=X":
                price_str = f"Rs.{s.price:.4f}"
            elif s.ticker == "CL=F":
                price_str = f"${s.price:.2f}"
            else:
                price_str = f"{s.price:,.2f}"
            lines.append(
                f"{s.change_emoji} <b>{s.name}</b>: "
                f"<code>{price_str}</code> "
                f"({s.sign}{s.change_pct:.2f}%)"
            )

        lines += ["", "🔔 <b>Today's Alerts</b>"]
        if total_alerts == 0:
            lines.append("  No alerts today — quiet session")
        else:
            lines.append(
                f"  <b>{total_alerts}</b> total  |  "
                f"<b>{len(critical_alerts)}</b> CRITICAL  |  "
                f"<b>{total_alerts - len(critical_alerts)}</b> NORMAL"
            )
            for a in alerts_today[-3:]:
                direction_arrow = "▲" if a["direction"] == "RISING" else "▼"
                sign = "+" if a["change"] > 0 else ""
                lines.append(
                    f"  {a['time']} — {a['name']} "
                    f"{direction_arrow} {sign}{a['change']:.2f}%"
                )

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Tomorrow: <b>{tomorrow_status}</b>",
            "<i>Market closed. See you tomorrow 🙏</i>",
        ]

        return "\n".join(lines)


# ── Main Briefing Service ──────────────────────────────────────────────────────

class BriefingService:
    """
    Orchestrates data fetching + formatting + sending
    for both morning briefing and EOD summary.

    Called from scheduler.py via two CronTrigger jobs:
      - 8:45 AM IST  → send_morning_briefing()
      - 3:35 PM IST  → send_eod_summary()
    """

    def __init__(self) -> None:
        self._settings  = get_settings()
        self._fetcher   = BriefingFetcher()
        self._formatter = BriefingFormatter()
        self._telegram  = TelegramNotifier()

    def send_morning_briefing(self) -> None:
        """
        Fetches all data and sends the pre-market briefing.
        Called at 8:45 AM IST by scheduler.
        Skips on holidays and weekends automatically.
        """
        now_ist  = datetime.now(IST)
        calendar = MarketCalendar()

        if not calendar.is_trading_day(now_ist.date()):
            logger.info("Morning briefing skipped — not a trading day")
            return

        date_str = now_ist.strftime("%A, %d %b %Y")
        logger.info("Sending morning briefing for %s", date_str)

        # Fetch all data
        indian         = self._fetcher.fetch_all_indian()
        global_signals = self._fetcher.fetch_all_global()
        alerts_yest    = self._fetcher.get_alerts_yesterday()
        upcoming_hols  = calendar.get_upcoming_holidays(days_ahead=14)

        # Tomorrow status
        actual_tomorrow = now_ist.date() + timedelta(days=1)
        next_trading    = calendar.get_next_trading_day(now_ist.date())
        next_str        = calendar.format_date(next_trading)

        if not calendar.is_trading_day(actual_tomorrow):
            reason          = calendar.get_closure_reason(actual_tomorrow)
            tomorrow_status = f"CLOSED ({reason}) — Next open: {next_str}"
        else:
            tomorrow_status = f"OPEN — {next_str}"

        # Send Email
        subject, body = self._formatter.format_morning_email(
            date_str=date_str,
            indian=indian,
            global_signals=global_signals,
            alerts_yesterday=alerts_yest,
            upcoming_holidays=upcoming_hols,
            tomorrow_status=tomorrow_status,
        )
        self._send_email(subject=subject, body=body)

        # Send Telegram
        tg_message = self._formatter.format_morning_telegram(
            date_str=date_str,
            indian=indian,
            global_signals=global_signals,
            alerts_yesterday=alerts_yest,
            upcoming_holidays=upcoming_hols,
            tomorrow_status=tomorrow_status,
        )
        result = self._telegram.send_system_message(tg_message)
        if result.success:
            logger.info("Morning briefing Telegram sent ✓")
        else:
            logger.warning(
                "Telegram morning briefing failed: %s",
                result.error_message
            )

    def send_eod_summary(self) -> None:
        """
        Fetches all data and sends the EOD summary.
        Called at 3:35 PM IST by scheduler.
        Skips on holidays and weekends automatically.
        """
        now_ist  = datetime.now(IST)
        calendar = MarketCalendar()

        if not calendar.is_trading_day(now_ist.date()):
            logger.info("EOD summary skipped — not a trading day")
            return

        date_str = now_ist.strftime("%A, %d %b %Y")
        logger.info("Sending EOD summary for %s", date_str)

        # Fetch all data
        indian         = self._fetcher.fetch_all_indian()
        global_signals = self._fetcher.fetch_all_global()
        alerts_today   = self._fetcher.get_alerts_today()

        # Tomorrow status
        actual_tomorrow = now_ist.date() + timedelta(days=1)
        next_trading    = calendar.get_next_trading_day(now_ist.date())
        next_str        = calendar.format_date(next_trading)

        if not calendar.is_trading_day(actual_tomorrow):
            reason          = calendar.get_closure_reason(actual_tomorrow)
            tomorrow_status = f"CLOSED ({reason}) — Next open: {next_str}"
        else:
            tomorrow_status = f"OPEN — {next_str}"

        # Send Email
        subject, body = self._formatter.format_eod_email(
            date_str=date_str,
            indian=indian,
            global_signals=global_signals,
            alerts_today=alerts_today,
            tomorrow_status=tomorrow_status,
        )
        self._send_email(subject=subject, body=body)

        # Send Telegram
        tg_message = self._formatter.format_eod_telegram(
            date_str=date_str,
            indian=indian,
            global_signals=global_signals,
            alerts_today=alerts_today,
            tomorrow_status=tomorrow_status,
        )
        result = self._telegram.send_system_message(tg_message)
        if result.success:
            logger.info("EOD summary Telegram sent ✓")
        else:
            logger.warning(
                "Telegram EOD summary failed: %s",
                result.error_message
            )

    def _send_email(self, *, subject: str, body: str) -> None:
        """Sends email via Gmail SMTP. Errors logged, never raised."""
        try:
            settings  = self._settings
            recipient = settings.ALERT_RECIPIENT_EMAIL

            msg            = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = settings.GMAIL_SENDER
            msg["To"]      = recipient
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(
                    settings.GMAIL_SENDER,
                    settings.GMAIL_APP_PASSWORD,
                )
                server.sendmail(
                    settings.GMAIL_SENDER,
                    [recipient],
                    msg.as_string(),
                )

            logger.info(
                "Briefing email sent to %s | %s", recipient, subject
            )

        except Exception as exc:
            logger.error(
                "Failed to send briefing email: %s", exc,
                exc_info=True
            )