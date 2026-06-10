"""
notifier.py
-----------
Email notification layer — sends alert emails via Gmail SMTP.

Responsibilities:
  - Format and send alert emails using Gmail App Password
  - Handle SMTP errors gracefully without crashing the system
  - Record every send attempt in the database (success or failure)
  - Return a NotificationResult so the scheduler knows what happened

Uses Python's built-in smtplib — no extra packages needed.
Connection method: SMTP_SSL on port 465 (most reliable for Gmail).
"""

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

logger = logging.getLogger(__name__)


# ── Notification Result ────────────────────────────────────────────────────────

@dataclass
class NotificationResult:
    """
    Returned by EmailNotifier.send() after every dispatch attempt.
    Tells the scheduler whether the email was sent and why it
    succeeded or failed — used for logging and database recording.
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
    Sends alert emails via Gmail SMTP using an App Password.

    Instantiate once and reuse — settings are loaded at creation
    and the SMTP connection is opened fresh for each email
    (keeps things simple and avoids stale connection issues).
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    def send(self, signal: AlertSignal) -> NotificationResult:
        """
        Send an alert email for the given signal.

        Steps:
          1. Build the email message (subject + body)
          2. Connect to Gmail SMTP and send
          3. Record the result in the database
          4. Return NotificationResult

        Never raises an exception — all errors are caught,
        logged, and returned in the NotificationResult.
        """
        recipient = self._settings.ALERT_RECIPIENT_EMAIL
        subject   = signal.format_email_subject()
        body      = signal.format_email_body()

        logger.info(
            "Sending alert email to %s | Subject: %s",
            recipient, subject
        )

        # ── Step 1 & 2: Build and send email ──────────────────────────────
        result = self._send_email(
            recipient=recipient,
            subject=subject,
            body=body,
        )

        # ── Step 3: Record in database ─────────────────────────────────────
        self._record_to_db(signal=signal, result=result)

        # ── Step 4: Log outcome ────────────────────────────────────────────
        if result.success:
            logger.info("Alert dispatched: %s", result)
        else:
            logger.error("Alert dispatch failed: %s", result)

        return result

    # ── Private: SMTP Send ─────────────────────────────────────────────────────

    def _send_email(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
    ) -> NotificationResult:
        """
        Builds the MIME email and sends it via Gmail SMTP SSL.
        Returns NotificationResult — never raises.
        """
        try:
            # Build the email message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = self._settings.GMAIL_SENDER
            msg["To"]      = recipient

            # Attach plain text body
            msg.attach(MIMEText(body, "plain", "utf-8"))

            # Connect and send via Gmail SMTP SSL (port 465)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(
                    self._settings.GMAIL_SENDER,
                    self._settings.GMAIL_APP_PASSWORD,
                )
                server.sendmail(
                    from_addr=self._settings.GMAIL_SENDER,
                    to_addrs=[recipient],
                    msg=msg.as_string(),
                )

            logger.debug(
                "SMTP send successful: %s → %s",
                self._settings.GMAIL_SENDER, recipient
            )
            return NotificationResult(success=True, recipient=recipient)

        except smtplib.SMTPAuthenticationError:
            error = (
                "Gmail authentication failed. "
                "Check GMAIL_SENDER and GMAIL_APP_PASSWORD in .env. "
                "Make sure the App Password has no spaces."
            )
            logger.error(error)
            return NotificationResult(
                success=False,
                recipient=recipient,
                error_message=error,
            )

        except smtplib.SMTPRecipientsRefused:
            error = f"Recipient address refused by Gmail: {recipient}"
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

    # ── Private: Database Recording ────────────────────────────────────────────

    def _record_to_db(
        self,
        *,
        signal: AlertSignal,
        result: NotificationResult,
    ) -> None:
        """
        Saves the alert dispatch attempt to the database.
        Called after every send — success or failure.
        If the DB write itself fails, logs the error and continues
        so a database issue never blocks email sending.
        """
        try:
            with get_session() as session:
                AlertRepository.save(
                    session,
                    ticker=signal.ticker,
                    display_name=signal.display_name,
                    direction=signal.direction.value,
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