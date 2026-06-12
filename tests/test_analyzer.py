
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.analyzer import AlertSignal, Direction, MarketAnalyzer
from src.fetcher import PriceData


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_price_data(ticker: str, price: float) -> PriceData:
    """Creates a PriceData object for testing."""
    return PriceData(
        ticker=ticker,
        price=price,
        volume=1_000_000,
        timestamp=datetime.now(timezone.utc),
        source="test",
    )


def make_signal(
    direction: Direction = Direction.RISING,
    change_pct: float = 1.5,
) -> AlertSignal:
    """Creates an AlertSignal object for testing."""
    return AlertSignal(
        ticker="^NSEI",
        display_name="Nifty 50 (NSE)",
        direction=direction,
        change_pct=change_pct,
        current_price=24360.00,
        previous_price=24000.00,
        price_source="test",
        generated_at=datetime.now(timezone.utc),
    )


# ── AlertSignal Tests ──────────────────────────────────────────────────────────

class TestAlertSignal:
    """Tests for the AlertSignal dataclass and its formatting methods."""

    def test_direction_arrow_rising(self):
        signal = make_signal(Direction.RISING)
        assert signal.direction_arrow == "▲"

    def test_direction_arrow_falling(self):
        signal = make_signal(Direction.FALLING, change_pct=-1.5)
        assert signal.direction_arrow == "▼"

    def test_abs_change_pct_positive(self):
        signal = make_signal(Direction.RISING, change_pct=2.5)
        assert signal.abs_change_pct == pytest.approx(2.5)

    def test_abs_change_pct_negative(self):
        signal = make_signal(Direction.FALLING, change_pct=-2.5)
        assert signal.abs_change_pct == pytest.approx(2.5)

    def test_email_subject_contains_ticker_name(self):
        signal = make_signal(Direction.RISING)
        subject = signal.format_email_subject()
        assert "Nifty 50 (NSE)" in subject

    def test_email_subject_contains_direction(self):
        signal = make_signal(Direction.RISING)
        subject = signal.format_email_subject()
        assert "RISING" in subject

    def test_email_subject_contains_alert_tag(self):
        signal = make_signal()
        subject = signal.format_email_subject()
        assert "[ALERT]" in subject

    def test_email_subject_rising_has_plus_sign(self):
        signal = make_signal(Direction.RISING, change_pct=1.5)
        subject = signal.format_email_subject()
        assert "+" in subject

    def test_email_subject_falling_has_minus_sign(self):
        signal = make_signal(Direction.FALLING, change_pct=-1.5)
        subject = signal.format_email_subject()
        assert "-" in subject

    def test_email_body_contains_current_price(self):
        signal = make_signal()
        body = signal.format_email_body()
        assert "24,360.00" in body

    def test_email_body_contains_previous_price(self):
        signal = make_signal()
        body = signal.format_email_body()
        assert "24,000.00" in body

    def test_email_body_contains_data_source(self):
        signal = make_signal()
        body = signal.format_email_body()
        assert "test" in body

    def test_signal_is_immutable(self):
        """frozen=True means fields cannot be changed after creation."""
        signal = make_signal()
        with pytest.raises(Exception):
            signal.change_pct = 99.0  # type: ignore


# ── Direction Enum Tests ───────────────────────────────────────────────────────

class TestDirection:
    """Tests for the Direction enum."""

    def test_rising_value(self):
        assert Direction.RISING.value == "RISING"

    def test_falling_value(self):
        assert Direction.FALLING.value == "FALLING"

    def test_flat_value(self):
        assert Direction.FLAT.value == "FLAT"

    def test_direction_is_string_comparable(self):
        """Direction inherits from str so == comparison with strings works."""
        assert Direction.RISING == "RISING"
        assert Direction.FALLING == "FALLING"


# ── MarketAnalyzer Tests ───────────────────────────────────────────────────────

class TestMarketAnalyzer:
    """
    Tests for MarketAnalyzer.analyze() logic.
    All database calls are mocked so no real DB is needed.
    """

    @patch("src.analyzer.PriceRepository.upsert_price")
    @patch("src.analyzer.PriceRepository.get_last_price", return_value=None)
    @patch("src.analyzer.get_session")
    def test_first_run_returns_none(
        self, mock_session, mock_get_price, mock_upsert
    ):
        """On first run with no stored price, should return None."""
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        analyzer = MarketAnalyzer()
        result = analyzer.analyze(make_price_data("^NSEI", 22000.0))
        assert result is None

    @patch("src.analyzer.AlertRepository.get_last_sent_time", return_value=None)
    @patch("src.analyzer.PriceRepository.upsert_price")
    @patch("src.analyzer.PriceRepository.get_last_price", return_value=22000.0)
    @patch("src.analyzer.get_session")
    def test_price_rise_above_threshold_generates_signal(
        self, mock_session, mock_get_price, mock_upsert, mock_last_alert
    ):
        """A 1% rise (above 0.5% threshold) should generate an AlertSignal."""
        ctx = MagicMock()
        mock_session.return_value.__enter__ = lambda s: ctx
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        analyzer = MarketAnalyzer()
        # 22220 is +1% from 22000 — above 0.5% threshold
        result = analyzer.analyze(make_price_data("^NSEI", 22220.0))

        assert result is not None
        assert result.direction == Direction.RISING
        assert result.change_pct == pytest.approx(1.0, rel=0.01)

    @patch("src.analyzer.AlertRepository.get_last_sent_time", return_value=None)
    @patch("src.analyzer.PriceRepository.upsert_price")
    @patch("src.analyzer.PriceRepository.get_last_price", return_value=22000.0)
    @patch("src.analyzer.get_session")
    def test_price_fall_above_threshold_generates_signal(
        self, mock_session, mock_get_price, mock_upsert, mock_last_alert
    ):
        """A -1% fall (above 0.5% threshold) should generate a FALLING signal."""
        ctx = MagicMock()
        mock_session.return_value.__enter__ = lambda s: ctx
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        analyzer = MarketAnalyzer()
        # 21780 is -1% from 22000
        result = analyzer.analyze(make_price_data("^NSEI", 21780.0))

        assert result is not None
        assert result.direction == Direction.FALLING
        assert result.change_pct == pytest.approx(-1.0, rel=0.01)

    @patch("src.analyzer.PriceRepository.upsert_price")
    @patch("src.analyzer.PriceRepository.get_last_price", return_value=22000.0)
    @patch("src.analyzer.get_session")
    def test_price_change_below_threshold_returns_none(
        self, mock_session, mock_get_price, mock_upsert
    ):
        """A 0.1% change (below 0.5% threshold) should return None."""
        ctx = MagicMock()
        mock_session.return_value.__enter__ = lambda s: ctx
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        analyzer = MarketAnalyzer()
        # 22022 is only +0.1% from 22000 — below threshold
        result = analyzer.analyze(make_price_data("^NSEI", 22022.0))
        assert result is None

    @patch("src.analyzer.AlertRepository.get_last_sent_time", return_value=None)
    @patch("src.analyzer.PriceRepository.upsert_price")
    @patch("src.analyzer.PriceRepository.get_last_price", return_value=22000.0)
    @patch("src.analyzer.get_session")
    def test_signal_has_correct_ticker(
        self, mock_session, mock_get_price, mock_upsert, mock_last_alert
    ):
        """Generated signal should carry the correct ticker symbol."""
        ctx = MagicMock()
        mock_session.return_value.__enter__ = lambda s: ctx
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        analyzer = MarketAnalyzer()
        result = analyzer.analyze(make_price_data("^NSEI", 22220.0))

        assert result is not None
        assert result.ticker == "^NSEI"
        assert result.display_name == "Nifty 50 (NSE)"

    @patch("src.analyzer.AlertRepository.get_last_sent_time", return_value=None)
    @patch("src.analyzer.PriceRepository.upsert_price")
    @patch("src.analyzer.PriceRepository.get_last_price", return_value=22000.0)
    @patch("src.analyzer.get_session")
    def test_signal_stores_both_prices(
        self, mock_session, mock_get_price, mock_upsert, mock_last_alert
    ):
        """Signal should store both current and previous prices."""
        ctx = MagicMock()
        mock_session.return_value.__enter__ = lambda s: ctx
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        analyzer = MarketAnalyzer()
        result = analyzer.analyze(make_price_data("^NSEI", 22220.0))

        assert result is not None
        assert result.current_price  == pytest.approx(22220.0)
        assert result.previous_price == pytest.approx(22000.0)