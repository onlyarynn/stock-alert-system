"""
database.py — Persistent storage using SQLite and SQLAlchemy ORM.
Stores alert history and last-seen prices.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float,
    Integer, String, create_engine, desc, func, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from .config import get_settings

logger = logging.getLogger(__name__)

Base = declarative_base()


# ── Table 1: Alert Records ─────────────────────────────────────────────────────

class AlertRecord(Base):
    __tablename__ = "alert_records"

    id             = Column(Integer,     primary_key=True, autoincrement=True)
    ticker         = Column(String(20),  nullable=False, index=True)
    display_name   = Column(String(100), nullable=False)
    direction      = Column(String(10),  nullable=False)
    alert_level    = Column(String(10),  nullable=False, default="NORMAL")
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
            f"level={self.alert_level} "
            f"direction={self.direction} "
            f"change={self.change_pct:.2f}%>"
        )


# ── Table 2: Price Snapshots ───────────────────────────────────────────────────

class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id          = Column(Integer,    primary_key=True, autoincrement=True)
    ticker      = Column(String(20), nullable=False, unique=True, index=True)
    price       = Column(Float,      nullable=False)
    recorded_at = Column(DateTime,   nullable=False)

    def __repr__(self) -> str:
        return f"<PriceSnapshot ticker={self.ticker} price={self.price}>"


# ── Engine & Session ───────────────────────────────────────────────────────────

_engine       = None
_SessionLocal = None


def init_db() -> None:
    global _engine, _SessionLocal
    settings = get_settings()
    db_path  = Path(settings.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    Base.metadata.create_all(bind=_engine)
    _SessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=_engine
    )
    logger.info("Database initialised at: %s", db_path)
    print(f"Database initialised at: {db_path}")


@contextmanager
def get_session():
    if _SessionLocal is None:
        raise RuntimeError("Call init_db() before get_session().")
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

    @staticmethod
    def save(
        session: Session,
        *,
        ticker: str,
        display_name: str,
        direction: str,
        alert_level: str = "NORMAL",
        change_pct: float,
        current_price: float,
        previous_price: float,
        email_sent_to: str,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> AlertRecord:
        record = AlertRecord(
            ticker=ticker,
            display_name=display_name,
            direction=direction,
            alert_level=alert_level,
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
        session: Session, ticker: str
    ) -> Optional[datetime]:
        """Last successful alert time for any level — used for normal cooldown."""
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
    def get_last_critical_alert_time(
        session: Session, ticker: str
    ) -> Optional[datetime]:
        """Last CRITICAL alert time — used for critical cooldown (5 min)."""
        row = (
            session.query(AlertRecord.sent_at)
            .filter(
                AlertRecord.ticker      == ticker,
                AlertRecord.success     == True,
                AlertRecord.alert_level == "CRITICAL",
            )
            .order_by(desc(AlertRecord.sent_at))
            .first()
        )
        return row[0] if row else None

    @staticmethod
    def get_total_sent_today(session: Session, ticker: str) -> int:
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

    @staticmethod
    def get_last_price(session: Session, ticker: str) -> Optional[float]:
        row = session.query(PriceSnapshot).filter_by(ticker=ticker).first()
        return row.price if row else None

    @staticmethod
    def upsert_price(session: Session, ticker: str, price: float) -> None:
        row = session.query(PriceSnapshot).filter_by(ticker=ticker).first()
        now = datetime.now(timezone.utc)
        if row:
            row.price       = price
            row.recorded_at = now
        else:
            session.add(PriceSnapshot(
                ticker=ticker, price=price, recorded_at=now
            ))
        logger.debug("Price snapshot: %s = %.2f", ticker, price)