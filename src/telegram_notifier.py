"""
Sends Telegram alerts via Bot API with automatic ISP bypass.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests
import urllib3

from .analyzer import AlertLevel, AlertSignal
from .config import get_settings

# Suppress SSL warnings for IP-based fallback endpoints
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ── Endpoint list — tried in order until one succeeds ─────────────────────────
# Each entry: (url_template, verify_ssl)
TELEGRAM_ENDPOINTS = [
    (
        "https://api.telegram.org/bot{token}/{method}",
        True,    # standard domain — SSL verified
    ),
    (
        "https://149.154.167.220/bot{token}/{method}",
        False,   # Telegram DC2 IP — SSL disabled (cert is for domain)
    ),
    (
        "https://149.154.175.50/bot{token}/{method}",
        False,   # Telegram DC4 IP — second fallback
    ),
]

MAX_MESSAGE_LENGTH = 4000   # Telegram limit is 4096; stay safely under


# ── Result Object ──────────────────────────────────────────────────────────────

@dataclass
class TelegramResult:
    """
    Returned after every Telegram send attempt.
    Mirrors NotificationResult structure for consistency.
    """
    success:       bool
    chat_id:       str
    error_message: Optional[str] = None

    def __str__(self) -> str:
        if self.success:
            return f"Telegram sent successfully to chat {self.chat_id}"
        return f"Telegram FAILED to chat {self.chat_id}: {self.error_message}"


# ── Telegram Notifier ──────────────────────────────────────────────────────────

class TelegramNotifier:
    """
    Sends alert messages to your Telegram chat via Bot API.

    Automatically tries multiple Telegram endpoints to bypass
    ISP-level blocks on api.telegram.org. Uses synchronous
    HTTP calls to fit naturally into the existing scheduler flow.

    Instantiate once and reuse across all alert cycles.
    """

    def __init__(self) -> None:
        self._settings       = get_settings()
        self._token          = self._settings.TELEGRAM_BOT_TOKEN
        self._chat_id        = self._settings.TELEGRAM_CHAT_ID
        self._enabled        = self._settings.telegram_enabled
        # Cache which endpoint worked last — avoids retrying failed ones
        self._working_endpoint_index: int = 0

        if self._enabled:
            logger.info(
                "TelegramNotifier initialised — chat_id: %s", self._chat_id
            )
        else:
            logger.warning(
                "TelegramNotifier disabled — "
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env"
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def send_alert(self, signal: AlertSignal) -> TelegramResult:
        """
        Send formatted alert for an AlertSignal.
        Called alongside EmailNotifier.send() from notifier.py.
        """
        if not self._enabled:
            return TelegramResult(
                success=False,
                chat_id="",
                error_message="Telegram not configured",
            )
        message = self._format_alert_message(signal)
        return self._send_message(message)

    def send_system_message(self, text: str) -> TelegramResult:
        """
        Send a plain system message.
        Used for briefings, market-closed notifications etc.
        """
        if not self._enabled:
            return TelegramResult(
                success=False,
                chat_id="",
                error_message="Telegram not configured",
            )
        return self._send_message(text)

    def test_connection(self) -> bool:
        """
        Sends a test message to verify token, chat ID and connectivity.
        Returns True if successful.
        """
        result = self.send_system_message(
            "✅ Stock Alert System connected to Telegram!\n"
            "You will now receive all alerts here."
        )
        return result.success

    # ── Message Formatters ─────────────────────────────────────────────────────

    def _format_alert_message(self, signal: AlertSignal) -> str:
        """Formats an AlertSignal into a clean Telegram HTML message."""
        sign      = "+" if signal.change_pct > 0 else ""
        arrow     = "▲" if signal.change_pct > 0 else "▼"
        pnl_emoji = "🟢" if signal.change_pct > 0 else "🔴"

        if signal.level == AlertLevel.CRITICAL:
            header = (
                "🚨 <b>CRITICAL ALERT</b> 🚨\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            footer = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <b>Act now — review your position immediately</b>"
            )
        else:
            header = (
                "🔔 <b>ALERT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            footer = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 Monitor the situation"
            )

        return (
            f"{header}\n"
            f"\n"
            f"{pnl_emoji} <b>{signal.display_name}</b>\n"
            f"\n"
            f"<b>Direction</b>  : {arrow} {signal.direction.value}\n"
            f"<b>Change</b>     : <code>{sign}{signal.change_pct:.2f}%</code>\n"
            f"<b>Current</b>    : <code>Rs.{signal.current_price:,.2f}</code>\n"
            f"<b>Previous</b>   : <code>Rs.{signal.previous_price:,.2f}</code>\n"
            f"<b>Move</b>       : <code>{signal.current_price - signal.previous_price:+,.2f} pts</code>\n"
            f"<b>Level</b>      : {signal.level.value}\n"
            f"<b>Time</b>       : {signal.ist_time_str}\n"
            f"\n"
            f"{footer}"
        )

    def format_holiday_reminder(
        self,
        holiday_name: str,
        holiday_date_str: str,
        next_trading_str: str,
    ) -> str:
        """Formats a holiday reminder message for Telegram."""
        return (
            f"📅 <b>Market Holiday Tomorrow</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"<b>Date</b>     : {holiday_date_str}\n"
            f"<b>Holiday</b>  : {holiday_name}\n"
            f"<b>Status</b>   : Market CLOSED 🔒\n"
            f"\n"
            f"📆 Next trading day: <b>{next_trading_str}</b>\n"
            f"\n"
            f"<i>The alert system will resume automatically.</i>"
        )

    # ── Private: Core Send ─────────────────────────────────────────────────────

    def _send_message(self, text: str) -> TelegramResult:
        """
        Send text to Telegram with automatic endpoint fallback.

        Strategy:
          1. Try cached working endpoint first (fast path)
          2. On failure, try all other endpoints in order
          3. Split messages longer than 4000 chars into chunks
          4. Cache the endpoint that worked for next time

        Never raises — all errors caught and returned.
        """
        if not text or not text.strip():
            return TelegramResult(
                success=True,
                chat_id=self._chat_id,
                error_message=None,
            )

        # Split long messages into chunks
        chunks = self._split_message(text)

        for chunk_idx, chunk in enumerate(chunks):
            result = self._send_chunk_with_fallback(chunk)
            if not result.success:
                return result
            # Small delay between chunks to avoid rate limiting
            if len(chunks) > 1 and chunk_idx < len(chunks) - 1:
                time.sleep(0.5)

        return TelegramResult(success=True, chat_id=self._chat_id)

    def _send_chunk_with_fallback(self, text: str) -> TelegramResult:
        """
        Tries all endpoints until one succeeds.
        Starts from the last known working endpoint.
        """
        # Build ordered list starting from last working endpoint
        endpoint_count = len(TELEGRAM_ENDPOINTS)
        ordered_indices = [
            (self._working_endpoint_index + i) % endpoint_count
            for i in range(endpoint_count)
        ]

        last_error = "All Telegram endpoints failed"

        for idx in ordered_indices:
            url_template, verify_ssl = TELEGRAM_ENDPOINTS[idx]
            url = url_template.format(
                token=self._token,
                method="sendMessage",
            )

            result = self._post_to_endpoint(
                url=url,
                text=text,
                verify_ssl=verify_ssl,
                endpoint_idx=idx,
            )

            if result.success:
                # Cache this working endpoint for next time
                if self._working_endpoint_index != idx:
                    logger.info(
                        "Telegram: switching to endpoint %d "
                        "(bypassing ISP block)",
                        idx,
                    )
                    self._working_endpoint_index = idx
                return result

            last_error = result.error_message or last_error

        return TelegramResult(
            success=False,
            chat_id=self._chat_id,
            error_message=last_error,
        )

    def _post_to_endpoint(
        self,
        *,
        url: str,
        text: str,
        verify_ssl: bool,
        endpoint_idx: int,
    ) -> TelegramResult:
        """
        Makes one HTTP POST attempt to a specific endpoint.
        Returns TelegramResult — never raises.
        """
        payload = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=30,
                verify=verify_ssl,
            )
            data = response.json()

            if response.status_code == 200 and data.get("ok"):
                logger.info(
                    "Telegram message sent via endpoint %d to chat %s",
                    endpoint_idx,
                    self._chat_id,
                )
                return TelegramResult(
                    success=True,
                    chat_id=self._chat_id,
                )

            # API returned an error response
            error = data.get("description", "Unknown Telegram API error")
            logger.warning(
                "Telegram endpoint %d API error: %s",
                endpoint_idx, error,
            )
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=error,
            )

        except requests.exceptions.SSLError as exc:
            logger.debug(
                "Telegram endpoint %d SSL error: %s", endpoint_idx, exc
            )
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=f"SSL error on endpoint {endpoint_idx}",
            )

        except requests.exceptions.ConnectionError as exc:
            logger.debug(
                "Telegram endpoint %d connection error: %s",
                endpoint_idx, exc,
            )
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=f"Connection error on endpoint {endpoint_idx}",
            )

        except requests.exceptions.Timeout:
            logger.debug(
                "Telegram endpoint %d timed out", endpoint_idx
            )
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=f"Timeout on endpoint {endpoint_idx}",
            )

        except Exception as exc:
            logger.debug(
                "Telegram endpoint %d unexpected error: %s",
                endpoint_idx, exc,
            )
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=str(exc),
            )

    # ── Private: Message Splitting ─────────────────────────────────────────────

    def _split_message(self, text: str) -> list[str]:
        """
        Splits text longer than MAX_MESSAGE_LENGTH into chunks.
        Splits on newlines to keep sections readable.
        """
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]

        chunks  = []
        current = ""

        for line in text.split("\n"):
            candidate = current + line + "\n"
            if len(candidate) > MAX_MESSAGE_LENGTH:
                if current.strip():
                    chunks.append(current.strip())
                current = line + "\n"
            else:
                current = candidate

        if current.strip():
            chunks.append(current.strip())

        logger.debug(
            "Message split into %d chunks (%d chars total)",
            len(chunks), len(text),
        )
        return chunks