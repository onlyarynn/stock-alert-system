
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from .analyzer import AlertLevel, AlertSignal
from .config import get_settings

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


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

    Uses `requests` for synchronous HTTP calls — fits naturally
    into your existing synchronous scheduler and notifier flow.

    Instantiate once and reuse across all alert cycles.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._token    = self._settings.TELEGRAM_BOT_TOKEN
        self._chat_id  = self._settings.TELEGRAM_CHAT_ID
        self._enabled  = self._settings.telegram_enabled

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
        Send a formatted alert message for the given AlertSignal.
        Called in parallel with EmailNotifier.send() from notifier.py.
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
        Send a plain system message (startup, shutdown, health check).
        Called from scheduler.py for non-alert notifications.
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
        Sends a test message to verify the bot token and chat ID work.
        Call this from main.py on startup if --test-telegram flag is set.
        Returns True if successful.
        """
        result = self.send_system_message(
            "✅ Stock Alert System connected to Telegram successfully!\n"
            "You will now receive all alerts here."
        )
        return result.success

    # ── Message Formatters ─────────────────────────────────────────────────────

    def _format_alert_message(self, signal: AlertSignal) -> str:
        """
        Formats an AlertSignal into a clean Telegram message.
        Telegram supports basic HTML formatting — we use it for bold/mono.
        """
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
                f"🔔 <b>ALERT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            footer = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 Monitor the situation"
            )

        message = (
            f"{header}\n"
            f"\n"
            f"{pnl_emoji} <b>{signal.display_name}</b>\n"
            f"\n"
            f"<b>Direction</b>  : {arrow} {signal.direction.value}\n"
            f"<b>Change</b>     : <code>{sign}{signal.change_pct:.2f}%</code>\n"
            f"<b>Current</b>    : <code>₹{signal.current_price:,.2f}</code>\n"
            f"<b>Previous</b>   : <code>₹{signal.previous_price:,.2f}</code>\n"
            f"<b>Move</b>       : <code>{signal.current_price - signal.previous_price:+,.2f} pts</code>\n"
            f"<b>Level</b>      : {signal.level.value}\n"
            f"<b>Time</b>       : {signal.ist_time_str}\n"
            f"\n"
            f"{footer}"
        )

        return message

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

    def format_eod_summary(
        self,
        date_str: str,
        prices: dict,
        alerts_today: list,
        tomorrow_status: str,
    ) -> str:
        """
        Formats an end-of-day summary message.
        Called from scheduler at 3:35 PM IST.
        """
        lines = [
            f"📈 <b>EOD Summary — {date_str}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        # Price summary
        for ticker, info in prices.items():
            emoji = "🟢" if info["change_pct"] >= 0 else "🔴"
            sign  = "+" if info["change_pct"] >= 0 else ""
            lines.append(
                f"{emoji} <b>{info['name']}</b>: "
                f"<code>₹{info['price']:,.2f}</code> "
                f"({sign}{info['change_pct']:.2f}%)"
            )

        lines.append("")
        lines.append(f"<b>Alerts today</b>: {len(alerts_today)}")

        if alerts_today:
            for alert in alerts_today[:5]:   # max 5 in summary
                lines.append(f"  • {alert}")

        lines.extend([
            "",
            f"<b>Tomorrow</b>: {tomorrow_status}",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
        ])

        return "\n".join(lines)

    # ── Private: API Call ──────────────────────────────────────────────────────

    def _send_message(self, text: str) -> TelegramResult:
        """
        Makes the actual HTTP POST to Telegram Bot API.
        Uses parse_mode=HTML for bold/monospace formatting.
        Never raises — all errors caught and returned.
        """
        url = TELEGRAM_API_BASE.format(
            token=self._token,
            method="sendMessage",
        )
        payload = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "HTML",   # enables <b>, <code>, <i> tags
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=10,   # 10 second timeout
            )
            data = response.json()

            if response.status_code == 200 and data.get("ok"):
                logger.info("Telegram message sent to chat %s", self._chat_id)
                return TelegramResult(success=True, chat_id=self._chat_id)

            # API returned an error
            error = data.get("description", "Unknown Telegram API error")
            logger.error("Telegram API error: %s", error)
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=error,
            )

        except requests.exceptions.ConnectionError:
            error = "No internet connection — cannot reach Telegram API"
            logger.error(error)
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=error,
            )

        except requests.exceptions.Timeout:
            error = "Telegram API request timed out after 10 seconds"
            logger.error(error)
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=error,
            )

        except Exception as exc:
            error = f"Unexpected error sending Telegram message: {exc}"
            logger.error(error, exc_info=True)
            return TelegramResult(
                success=False,
                chat_id=self._chat_id,
                error_message=error,
            )