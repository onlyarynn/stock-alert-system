"""
fetcher.py
----------
Market data layer — downloads live prices from Yahoo Finance.

Fetches Nifty 50 (^NSEI) and Sensex (^BSESN) prices using the
yfinance library. Includes retry logic with exponential back-off
so temporary network issues don't crash the monitoring system.

Note: yFinance provides data with approximately 15-minute delay
on the free tier. This is acceptable for swing-level alerts.
For real-time data, the AngelOneProvider stub can be implemented.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


# ── Price Data Object ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PriceData:
    """
    Immutable container for one price fetch result.

    All other modules work with PriceData objects — never raw floats.
    frozen=True means the values cannot be changed after creation,
    which prevents accidental modification downstream.
    """
    ticker:    str
    price:     float
    volume:    int
    timestamp: datetime
    source:    str

    def is_stale(self, max_age_minutes: int = 600) -> bool:
        """
        Returns True if this price data is older than max_age_minutes.
        Used to detect and discard outdated data before analysis.
        """
        now = datetime.now(timezone.utc)
        # Handle both timezone-aware and naive timestamps
        if self.timestamp.tzinfo is None:
            ts = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            ts = self.timestamp
        age_minutes = (now - ts).total_seconds() / 60
        return age_minutes > max_age_minutes

    def __str__(self) -> str:
        return (
            f"{self.ticker}: {self.price:,.2f} "
            f"(vol={self.volume:,}, source={self.source})"
        )


# ── Yahoo Finance Provider ─────────────────────────────────────────────────────

class YFinanceProvider:
    """
    Fetches intraday price data from Yahoo Finance.

    Uses 5-minute interval data for the current trading day
    and returns the most recent closing price available.
    Works for both NSE (^NSEI) and BSE (^BSESN) indices.
    """

    NAME = "yfinance"

    def fetch(self, ticker: str) -> Optional[PriceData]:
        """
        Download latest price for the given ticker symbol.
        Returns a PriceData object or None if fetch fails.
        """
        try:
            t   = yf.Ticker(ticker)
            df  = t.history(period="1d", interval="5m", auto_adjust=True)

            if df is None or df.empty:
                logger.warning(
                    "[%s] Empty dataframe returned for %s",
                    self.NAME, ticker
                )
                return None

            last_row = df.iloc[-1]
            price    = float(last_row["Close"])
            volume   = int(last_row.get("Volume", 0))

            if price <= 0:
                logger.warning(
                    "[%s] Invalid price %.2f for %s — skipping",
                    self.NAME, price, ticker
                )
                return None

            # yFinance returns a timezone-aware pandas Timestamp
            raw_ts = df.index[-1]
            if hasattr(raw_ts, "to_pydatetime"):
                ts = raw_ts.to_pydatetime()
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            return PriceData(
                ticker=ticker,
                price=price,
                volume=volume,
                timestamp=ts,
                source=self.NAME,
            )

        except Exception as exc:
            logger.error(
                "[%s] Fetch failed for %s: %s",
                self.NAME, ticker, exc,
                exc_info=True
            )
            return None


# ── Market Data Fetcher (with retry logic) ─────────────────────────────────────

class MarketDataFetcher:
    """
    Wraps any price provider with retry logic and stale data detection.

    On failure, waits with exponential back-off before retrying:
      Attempt 1 fails → wait 2s → Attempt 2
      Attempt 2 fails → wait 4s → Attempt 3
      Attempt 3 fails → return None, log error

    This handles temporary Yahoo Finance API issues gracefully
    without crashing the monitoring loop.
    """

    def __init__(
        self,
        provider: YFinanceProvider,
        max_retries: int = 3,
        base_delay_seconds: float = 2.0,
    ):
        self._provider          = provider
        self._max_retries       = max_retries
        self._base_delay        = base_delay_seconds

    def fetch_with_retry(self, ticker: str) -> Optional[PriceData]:
        """
        Fetch price for one ticker with automatic retry on failure.
        Returns PriceData on success or None if all attempts fail.
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            logger.debug(
                "Fetching %s — attempt %d/%d",
                ticker, attempt, self._max_retries
            )
            try:
                data = self._provider.fetch(ticker)

                if data is None:
                    raise ValueError(
                        f"Provider returned no data for {ticker}"
                    )

                if data.is_stale():
                    logger.warning(
                        "Stale data received for %s (ts=%s) — skipping",
                        ticker, data.timestamp
                    )
                    return None

                logger.info(
                    "Fetched: %s", data
                )
                return data

            except Exception as exc:
                last_error = exc
                wait_time  = self._base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Attempt %d/%d failed for %s: %s",
                    attempt, self._max_retries, ticker, exc
                )
                if attempt < self._max_retries:
                    logger.debug("Waiting %.0fs before retry…", wait_time)
                    time.sleep(wait_time)

        logger.error(
            "All %d attempts failed for %s. Last error: %s",
            self._max_retries, ticker, last_error
        )
        return None

    def fetch_all(
        self,
        tickers: list[str]
    ) -> dict[str, Optional[PriceData]]:
        """
        Fetch prices for all tickers in the watchlist.
        Returns a dict mapping ticker symbol → PriceData (or None).

        Example return value:
            {
                "^NSEI":  PriceData(ticker="^NSEI", price=22345.6, ...),
                "^BSESN": PriceData(ticker="^BSESN", price=73821.4, ...),
            }
        """
        results: dict[str, Optional[PriceData]] = {}
        for ticker in tickers:
            results[ticker] = self.fetch_with_retry(ticker)
        return results


# ── Factory Function ───────────────────────────────────────────────────────────

def create_fetcher() -> MarketDataFetcher:
    """
    Creates and returns a ready-to-use MarketDataFetcher.
    Called once from main.py at startup.
    """
    provider = YFinanceProvider()
    logger.info("Market data provider: %s", provider.NAME)
    return MarketDataFetcher(provider=provider)

