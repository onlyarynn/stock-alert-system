from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .analyzer import AlertSignal
from .config import get_settings
from .database import AlertRepository, get_session
from .telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


# ── Notification Result ────────────────────────────────────────────────────────

@dataclass
class NotificationResult:
    """
    Returned after every dispatch attempt.
    success=True means at least the email was sent.
    """
    success:       bool
    recipient:     str
    error_message: Optional[str] = None

    def __str__(self) -> str:
        if self.success:
            return f"Email sent successfully to {self.recipient}"
        return f"Email FAILED to {self.recipient}: {self.error_message}"


# ── Email Notifier ─────────────────────────────────────────────────────────────

class EmailNotifier:
    """
    Sends all alert emails via Gmail SMTP.
    Fires Telegram alerts in parallel when configured.

    Instantiate once and reuse — the SMTP connection is
    opened fresh for each email to avoid stale connections.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._telegram = TelegramNotifier()

    # ── Public: Price change alert ─────────────────────────────────────────────

    def send(self, signal: AlertSignal) -> NotificationResult:
        """
        Send alert email + Telegram for a price change signal.
        Records result in database. Never raises.
        """
        recipient = self._settings.ALERT_RECIPIENT_EMAIL
        subject   = signal.format_email_subject()
        body      = signal.format_email_body()

        logger.info(
            "Sending alert email to %s | Subject: %s",
            recipient, subject
        )

        # ── Email ──────────────────────────────────────────────────────────
        result = self._send_email(
            recipient=recipient,
            subject=subject,
            body=body,
        )

        # ── Telegram (failure never blocks email result) ───────────────────
        tg_result = self._telegram.send_alert(signal)
        if tg_result.success:
            logger.info("Telegram alert sent ✓")
        else:
            logger.warning(
                "Telegram alert failed (non-critical): %s",
                tg_result.error_message
            )

        # ── Database ───────────────────────────────────────────────────────
        self._record_to_db(signal=signal, result=result)

        if result.success:
            logger.info("Alert dispatched: %s", result)
        else:
            logger.error("Alert dispatch failed: %s", result)

        return result

    # ── Public: Market closed notification ────────────────────────────────────

    def send_market_closed_email(
        self,
        reason: str,
        next_trading_day: str,
        upcoming_holidays: list,
    ) -> NotificationResult:
        """
        Sends a single notification email when market is closed.
        Called once per closed day by the scheduler.
        """
        recipient = self._settings.ALERT_RECIPIENT_EMAIL
        subject   = f"[MARKET CLOSED] {reason} — Resumes {next_trading_day}"

        if upcoming_holidays:
            holiday_lines = "\n".join(
                f"  - {d.strftime('%d %b %Y')} — {name}"
                for d, name in upcoming_holidays
            )
            upcoming_section = (
                f"Upcoming market holidays (next 30 days):\n"
                f"{holiday_lines}"
            )
        else:
            upcoming_section = "No further holidays in the next 30 days."

        separator = "=" * 45
        body = f"""
{separator}
  MARKET CLOSED — Stock Alert System
{separator}

  Status        : Market CLOSED today
  Reason        : {reason}
  Next Open Day : {next_trading_day}

{separator}
  Monitoring paused for today.
  System will auto-resume on next trading day.

{upcoming_section}

{separator}
This is an automated message from your Stock Alert System.
{separator}
        """.strip()

        logger.info("Sending market closed notification: %s", reason)
        result = self._send_email(
            recipient=recipient,
            subject=subject,
            body=body,
        )
        if result.success:
            logger.info("Market closed email sent successfully.")
        else:
            logger.error(
                "Failed to send market closed email: %s",
                result.error_message
            )
        return result

    # ── Public: S/R level alert ────────────────────────────────────────────────

    def send_level_alert(self, alert) -> NotificationResult:
        """
        Sends a support/resistance level alert via email AND Telegram.
        Uses a distinct subject line format from normal price alerts.
        """
        recipient = self._settings.ALERT_RECIPIENT_EMAIL
        subject   = alert.format_email_subject()
        body      = alert.format_email_body()

        logger.info(
            "Sending level alert to %s | Subject: %s",
            recipient, subject
        )

        # ── Email ──────────────────────────────────────────────────────────
        result = self._send_email(
            recipient=recipient,
            subject=subject,
            body=body,
        )
        if result.success:
            logger.info("Level alert email sent successfully.")
        else:
            logger.error(
                "Level alert email failed: %s", result.error_message
            )

        # ── Telegram ───────────────────────────────────────────────────────
        tg_message = self._format_level_alert_telegram(alert)
        tg_result  = self._telegram._send_message(tg_message)
        if tg_result.success:
            logger.info("Level alert Telegram sent ✓")
        else:
            logger.warning(
                "Level alert Telegram failed (non-critical): %s",
                tg_result.error_message
            )

        return result

    # ── Public: VIX spike alert (NEW) ─────────────────────────────────────────

    def send_vix_spike_alert(self, vix_snap) -> NotificationResult:
        """
        Sends a dedicated India VIX spike alert via email AND Telegram.

        Fired by the scheduler when India VIX rises more than 15%
        from previous close in a single session — a reliable leading
        indicator of an imminent Nifty 50 sell-off.

        Args:
            vix_snap: VixSnapshot object from src/vix.py
        """
        from .vix import VixAlertChecker

        checker   = VixAlertChecker()
        recipient = self._settings.ALERT_RECIPIENT_EMAIL
        subject   = checker.format_spike_email_subject(vix_snap)
        body      = checker.format_spike_email_body(vix_snap)

        logger.warning(
            "Sending VIX spike alert | VIX=%.2f (%+.1f%%)",
            vix_snap.value, vix_snap.change_pct,
        )

        # ── Email ──────────────────────────────────────────────────────────
        result = self._send_email(
            recipient=recipient,
            subject=subject,
            body=body,
        )
        if result.success:
            logger.warning(
                "VIX spike alert email sent ✓ | VIX=%.2f",
                vix_snap.value,
            )
        else:
            logger.error(
                "VIX spike alert email FAILED: %s",
                result.error_message,
            )

        # ── Telegram ───────────────────────────────────────────────────────
        tg_message = checker.format_spike_telegram(vix_snap)
        tg_result  = self._telegram._send_message(tg_message)
        if tg_result.success:
            logger.warning("VIX spike alert Telegram sent ✓")
        else:
            logger.warning(
                "VIX spike Telegram failed (non-critical): %s",
                tg_result.error_message,
            )

        return result

    # ── Private: Level alert Telegram formatter ────────────────────────────────

    def _format_level_alert_telegram(self, alert) -> str:
        """Formats a support/resistance level alert for Telegram HTML."""
        from .levels import LevelAlertType

        if alert.alert_type == LevelAlertType.BREAKOUT:
            header = "🟢 <b>BREAKOUT ALERT</b>"
            action = "Price broke ABOVE resistance — possible bullish move"
        elif alert.alert_type == LevelAlertType.BREAKDOWN:
            header = "🔴 <b>BREAKDOWN ALERT</b>"
            action = "Price broke BELOW support — possible bearish move"
        else:
            if alert.level_type.value == "RESISTANCE":
                header = "⚠️ <b>APPROACHING RESISTANCE</b>"
                action = "Watch for rejection or breakout above this level"
            else:
                header = "⚠️ <b>APPROACHING SUPPORT</b>"
                action = "Watch for bounce or breakdown below this level"

        return (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"📊 <b>{alert.display_name}</b>\n"
            f"\n"
            f"<b>Level</b>      : {alert.level_name} "
            f"({alert.level_type.value})\n"
            f"<b>Level Price</b>: "
            f"<code>Rs.{alert.level_price:,.2f}</code>\n"
            f"<b>Current</b>    : "
            f"<code>Rs.{alert.current_price:,.2f}</code>\n"
            f"<b>Distance</b>   : "
            f"<code>{abs(alert.distance_pct):.3f}%</code>\n"
            f"\n"
            f"💡 {action}\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>All Levels Today</b>\n"
            f"R3: <code>{alert.levels.r3:,.2f}</code>  "
            f"R2: <code>{alert.levels.r2:,.2f}</code>  "
            f"R1: <code>{alert.levels.r1:,.2f}</code>\n"
            f"PP: <code>{alert.levels.pivot:,.2f}</code>\n"
            f"S1: <code>{alert.levels.s1:,.2f}</code>  "
            f"S2: <code>{alert.levels.s2:,.2f}</code>  "
            f"S3: <code>{alert.levels.s3:,.2f}</code>"
        )

    # ── Private: SMTP ─────────────────────────────────────────────────────────

    def _send_email(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
    ) -> NotificationResult:
        """
        Builds MIME email and sends via Gmail SMTP SSL port 465.
        Sends to ALL configured recipients (primary + secondary).
        Returns success if at least one recipient received the email.
        """
        all_recipients = self._settings.all_recipients

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = self._settings.GMAIL_SENDER
            msg["To"]      = ", ".join(all_recipients)
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(
                    self._settings.GMAIL_SENDER,
                    self._settings.GMAIL_APP_PASSWORD,
                )
                server.sendmail(
                    from_addr=self._settings.GMAIL_SENDER,
                    to_addrs=all_recipients,
                    msg=msg.as_string(),
                )

            logger.info(
                "Email sent to %d recipient(s): %s",
                len(all_recipients),
                ", ".join(all_recipients),
            )
            # Return result with primary recipient for DB recording
            return NotificationResult(
                success=True,
                recipient=", ".join(all_recipients),
            )

        except smtplib.SMTPAuthenticationError:
            error = (
                "Gmail authentication failed. "
                "Check GMAIL_SENDER and GMAIL_APP_PASSWORD in .env."
            )
            logger.error(error)
            return NotificationResult(
                success=False,
                recipient=recipient,
                error_message=error,
            )

        except smtplib.SMTPRecipientsRefused as exc:
            error = f"One or more recipients refused by Gmail: {exc}"
            logger.error(error)
            return NotificationResult(
                success=False,
                recipient=recipient,
                error_message=error,
            )

        except smtplib.SMTPException as exc:
            error = f"SMTP error: {exc}"
            logger.error(error)
            return NotificationResult(
                success=False,
                recipient=recipient,
                error_message=error,
            )

        except OSError as exc:
            error = (
                f"Network error connecting to smtp.gmail.com: {exc}. "
                "Check your internet connection."
            )
            logger.error(error)
            return NotificationResult(
                success=False,
                recipient=recipient,
                error_message=error,
            )

        except Exception as exc:
            error = f"Unexpected error sending email: {exc}"
            logger.error(error, exc_info=True)
            return NotificationResult(
                success=False,
                recipient=recipient,
                error_message=error,
            )
    
    # ── Private: Database ─────────────────────────────────────────────────────

    def _record_to_db(
        self,
        *,
        signal: AlertSignal,
        result: NotificationResult,
    ) -> None:
        """Saves every dispatch attempt to database — success or failure."""
        try:
            with get_session() as session:
                AlertRepository.save(
                    session,
                    ticker=signal.ticker,
                    display_name=signal.display_name,
                    direction=signal.direction.value,
                    alert_level=signal.level.value,
                    change_pct=signal.change_pct,
                    current_price=signal.current_price,
                    previous_price=signal.previous_price,
                    email_sent_to=result.recipient,
                    success=result.success,
                    error_message=result.error_message,
                )
        except Exception as exc:
            logger.error(
                "Failed to record alert in database: %s", exc,
                exc_info=True
            )