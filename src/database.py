"""
database.py
-----------
Persistent storage layer using SQLite and SQLAlchemy ORM.

Responsibilities:
  - Create and manage database tables on first run
  - Store every alert sent (full audit history)
  - Track last-seen price per ticker (prevents false alerts on restart)
  - Provide clean repository classes for reading and writing data
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    desc,
    func,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from .config import get_settings

logger = logging.getLogger(__name__)


# ── ORM Base ───────────────────────────────────────────────────────────────────
# Using declarative_base() instead of DeclarativeBase class
# for compatibility with SQLAlchemy 2.0 on all platforms

Base = declarative_base()


# ── Table 1: Alert Records ─────────────────────────────────────────────────────

class AlertRecord(Base):
    """
    One row inserted every time an alert email is dispatched.
    Used for cooldown tracking and full audit history.
    """

    __tablename__ = "alert_records"

    id             = Column(Integer,  primary_key=True, autoincrement=True)
    ticker         = Column(String(20),  nullable=False, index=True)
    display_name   = Column(String(100), nullable=False)
    direction      = Column(String(10),  nullable=False)
    change_pct     = Column(Float,       nullable=False)
    current_price  = Column(Float,       nullable=False)
    previous_price = Column(Float,       nullable=False)
    email_sent_to  = Column(String(200), nullable=False)
    sent_at        = Column(DateTime,    nullable=False)
    success        = Column(Boolean,     nullable=False, default=True)
    error_message  = Column(String(500), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AlertRecord ticker={self.ticker} "
            f"direction={self.direction} "
            f"change={self.change_pct:.2f}% "
            f"sent_at={self.sent_at}>"
        )


# ── Table 2: Price Snapshots ───────────────────────────────────────────────────

class PriceSnapshot(Base):
    """
    Stores the most recent price seen for each ticker.
    On restart the analyzer reads this instead of waiting
    for two consecutive live prices — prevents false alerts.
    """

    __tablename__ = "price_snapshots"

    id          = Column(Integer,   primary_key=True, autoincrement=True)
    ticker      = Column(String(20), nullable=False, unique=True, index=True)
    price       = Column(Float,     nullable=False)
    recorded_at = Column(DateTime,  nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PriceSnapshot ticker={self.ticker} "
            f"price={self.price}>"
        )


# ── Engine & Session Setup ─────────────────────────────────────────────────────

_engine       = None
_SessionLocal = None


def init_db() -> None:
    """
    Initialise the database engine and create all tables.
    Call this ONCE from main.py at startup.
    Creates the data/ directory and alerts.db automatically.
    """
    global _engine, _SessionLocal

    settings = get_settings()
    db_path  = Path(settings.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db_url  = f"sqlite:///{db_path}"
    _engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )

    Base.metadata.create_all(bind=_engine)

    _SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=_engine,
    )

    logger.info("Database initialised at: %s", db_path)
    print(f"Database initialised at: {db_path}")


@contextmanager
def get_session():
    """
    Provides a database session as a context manager.

    Usage:
        with get_session() as session:
            PriceRepository.upsert_price(session, ticker, price)

    Commits on success, rolls back on any exception.
    """
    if _SessionLocal is None:
        raise RuntimeError(
            "Database not initialised. "
            "Call init_db() before using get_session()."
        )
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Alert Repository ───────────────────────────────────────────────────────────

class AlertRepository:
    """
    All read/write operations for the alert_records table.
    Never write raw SQL elsewhere — use these methods only.
    """

    @staticmethod
    def save(
        session: Session,
        *,
        ticker: str,
        display_name: str,
        direction: str,
        change_pct: float,
        current_price: float,
        previous_price: float,
        email_sent_to: str,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> AlertRecord:
        """Insert a new alert record and return it."""
        record = AlertRecord(
            ticker=ticker,
            display_name=display_name,
            direction=direction,
            change_pct=round(change_pct, 4),
            current_price=round(current_price, 2),
            previous_price=round(previous_price, 2),
            email_sent_to=email_sent_to,
            sent_at=datetime.now(timezone.utc),
            success=success,
            error_message=error_message,
        )
        session.add(record)
        session.flush()
        logger.debug("Alert record saved: %r", record)
        return record

    @staticmethod
    def get_last_sent_time(
        session: Session,
        ticker: str,
    ) -> Optional[datetime]:
        """
        Returns UTC datetime of the most recent successful alert
        for this ticker, or None if no alert has been sent yet.
        """
        row = (
            session.query(AlertRecord.sent_at)
            .filter(
                AlertRecord.ticker  == ticker,
                AlertRecord.success == True,
            )
            .order_by(desc(AlertRecord.sent_at))
            .first()
        )
        return row[0] if row else None

    @staticmethod
    def get_total_sent_today(
        session: Session,
        ticker: str,
    ) -> int:
        """Returns how many alerts were sent today for this ticker."""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        count = (
            session.query(func.count(AlertRecord.id))
            .filter(
                AlertRecord.ticker  == ticker,
                AlertRecord.sent_at >= today_start,
            )
            .scalar()
        )
        return count or 0


# ── Price Repository ───────────────────────────────────────────────────────────

class PriceRepository:
    """
    All read/write operations for the price_snapshots table.
    """

    @staticmethod
    def get_last_price(
        session: Session,
        ticker: str,
    ) -> Optional[float]:
        """
        Returns last stored price for this ticker,
        or None if no price stored yet (first run).
        """
        row = (
            session.query(PriceSnapshot)
            .filter_by(ticker=ticker)
            .first()
        )
        return row.price if row else None

    @staticmethod
    def upsert_price(
        session: Session,
        ticker: str,
        price: float,
    ) -> None:
        """
        Insert or update price snapshot for this ticker.
        Called every poll cycle regardless of whether an alert fired.
        """
        row = (
            session.query(PriceSnapshot)
            .filter_by(ticker=ticker)
            .first()
        )
        now = datetime.now(timezone.utc)
        if row:
            row.price       = price
            row.recorded_at = now
        else:
            session.add(PriceSnapshot(
                ticker=ticker,
                price=price,
                recorded_at=now,
            ))
        logger.debug("Price snapshot updated: %s = %.2f", ticker, price)