"""
config.py
---------
Central configuration for the Stock Alert System.
All settings are loaded from the .env file and validated at startup.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Gmail credentials ─────────────────────────────────────────────
    GMAIL_SENDER: str = Field(
        ...,
        description="Gmail address used to send alert emails"
    )
    GMAIL_APP_PASSWORD: str = Field(
        ...,
        description="Gmail App Password (16 characters, no spaces)"
    )
    ALERT_RECIPIENT_EMAIL: str = Field(
        ...,
        description="Email address that receives alert emails"
    )

    # ── Market monitoring ─────────────────────────────────────────────
    POLL_INTERVAL_SECONDS: int = Field(default=300, ge=60, le=3600)
    ALERT_THRESHOLD_PCT: float = Field(default=0.5, gt=0.0, le=20.0)
    COOLDOWN_MINUTES: int = Field(default=30, ge=5)

    # ── Watchlist stored as plain string, parsed by property below ────
    WATCHLIST: str = Field(default="^NSEI,^BSESN")

    # ── Market hours (IST, 24-hour) ───────────────────────────────────
    MARKET_OPEN_HOUR: int = Field(default=9)
    MARKET_OPEN_MINUTE: int = Field(default=15)
    MARKET_CLOSE_HOUR: int = Field(default=15)
    MARKET_CLOSE_MINUTE: int = Field(default=30)

    # ── Storage and logging ───────────────────────────────────────────
    DB_PATH: str = Field(default="data/alerts.db")
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="logs/stock_alert.log")

    # ── Validators ────────────────────────────────────────────────────

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(
                f"LOG_LEVEL must be one of {valid}, got '{v}'"
            )
        return upper

    @field_validator("GMAIL_APP_PASSWORD")
    @classmethod
    def validate_app_password(cls, v: str) -> str:
        cleaned = v.replace(" ", "")
        if len(cleaned) != 16:
            raise ValueError(
                f"GMAIL_APP_PASSWORD must be 16 characters, "
                f"got {len(cleaned)}. Re-copy from Google Account."
            )
        return cleaned

    # ── Helper property ───────────────────────────────────────────────

    @property
    def watchlist_tickers(self) -> list[str]:
        """
        Returns WATCHLIST as a clean Python list.
        Handles both  ^NSEI,^BSESN  and  "^NSEI,^BSESN"  formats.
        Use this property everywhere instead of settings.WATCHLIST directly.
        """
        raw = self.WATCHLIST.strip().strip('"').strip("'")
        return [t.strip() for t in raw.split(",") if t.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns a cached Settings singleton.
    .env is read and validated exactly once at startup.
    """
    return Settings()


# ── Display names for ticker symbols ──────────────────────────────────
TICKER_DISPLAY_NAMES: dict[str, str] = {
    "^NSEI":  "Nifty 50 (NSE)",
    "^BSESN": "Sensex (BSE)",
}