from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path


# ── Argument Parser ────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-alert",
        description=(
            "Stock Market Alert System — "
            "monitors Nifty 50 & Sensex and sends email alerts"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py
  python main.py --dry-run
  python main.py --log-level DEBUG
  python main.py --threshold 1.0 --cooldown 60
  python main.py --once
        """,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Fetch and analyse prices but do NOT send any emails",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level from .env (default: INFO)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        metavar="PCT",
        help="Override alert threshold %% (e.g. 0.5 means 0.5%%)",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Override cooldown minutes between alerts (e.g. 30)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run exactly one monitoring cycle then exit",
    )

    return parser


# ── Logging Setup ──────────────────────────────────────────────────────────────

def setup_logging(log_level: str, log_file: str) -> None:
    """
    Configure root logger with two handlers:
      - Console : INFO level and above, clean format
      - File    : DEBUG level and above, full detail, rotating 10MB x 5 files

    All modules use logging.getLogger(__name__) — they all
    feed into this root logger automatically.
    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    log_format = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Console handler — INFO and above
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level, logging.INFO))
    console_handler.setFormatter(log_format)
    root_logger.addHandler(console_handler)

    # Rotating file handler — DEBUG and above (full detail)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=10 * 1024 * 1024,   # 10 MB per file
        backupCount=5,                # keep last 5 log files
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(log_format)
    root_logger.addHandler(file_handler)


# ── Startup Banner ─────────────────────────────────────────────────────────────

def print_banner(settings, dry_run: bool) -> None:
    """Prints a clear summary of all active settings at startup."""
    logger = logging.getLogger(__name__)
    w = 55
    logger.info("=" * w)
    logger.info("  Stock Alert Automation System  v1.0.0")
    logger.info("=" * w)
    logger.info("  Sender email  : %s", settings.GMAIL_SENDER)
    logger.info("  Alert sent to : %s", settings.ALERT_RECIPIENT_EMAIL)
    logger.info("  Watchlist     : %s", settings.watchlist_tickers)
    logger.info("  Threshold     : %.2f%%", settings.ALERT_THRESHOLD_PCT)
    logger.info("  Cooldown      : %d min", settings.COOLDOWN_MINUTES)
    logger.info("  Poll interval : %ds", settings.POLL_INTERVAL_SECONDS)
    logger.info("  Market hours  : %02d:%02d – %02d:%02d IST",
                settings.MARKET_OPEN_HOUR, settings.MARKET_OPEN_MINUTE,
                settings.MARKET_CLOSE_HOUR, settings.MARKET_CLOSE_MINUTE)
    logger.info("  Database      : %s", settings.DB_PATH)
    logger.info("  Log file      : %s", settings.LOG_FILE)
    if dry_run:
        logger.info("  *** DRY-RUN MODE — emails will NOT be sent ***")
    logger.info("=" * w)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:

    # ── Step 1: Parse arguments ────────────────────────────────────────────
    parser = build_arg_parser()
    args   = parser.parse_args()

    # Apply CLI overrides to environment BEFORE settings are loaded
    # (Settings are cached after first call — must set env vars first)
    if args.threshold is not None:
        os.environ["ALERT_THRESHOLD_PCT"] = str(args.threshold)
    if args.cooldown is not None:
        os.environ["COOLDOWN_MINUTES"] = str(args.cooldown)
    if args.log_level:
        os.environ["LOG_LEVEL"] = args.log_level

    # ── Step 2: Load and validate settings ────────────────────────────────
    # Import here so env overrides above take effect before caching
    from src.config import get_settings
    settings = get_settings()

    # ── Step 3: Setup logging ──────────────────────────────────────────────
    setup_logging(
        log_level=settings.LOG_LEVEL,
        log_file=settings.LOG_FILE,
    )
    logger = logging.getLogger(__name__)

    # ── Step 4: Print startup banner ───────────────────────────────────────
    print_banner(settings, dry_run=args.dry_run)

    # ── Step 5: Initialise database ────────────────────────────────────────
    from src.database import init_db
    init_db()

    # ── Step 6: Create scheduler ───────────────────────────────────────────
    from src.scheduler import StockAlertScheduler
    scheduler = StockAlertScheduler()

    # ── Dry-run mode: patch notifier to skip actual sending ────────────────
    if args.dry_run:
        logger.warning(
            "DRY-RUN MODE active — "
            "prices will be fetched and analysed but NO emails sent"
        )
        # Replace the send method with a no-op that logs instead
        from src.notifier import NotificationResult
        def dry_run_send(signal):
            logger.info(
                "[DRY-RUN] Would send: %s",
                signal.format_email_subject()
            )
            return NotificationResult(
                success=True,
                recipient=settings.ALERT_RECIPIENT_EMAIL,
            )
        scheduler._notifier.send = dry_run_send

    # ── Step 7: Run ────────────────────────────────────────────────────────
    if args.once:
        # Single cycle mode — run once and exit
        logger.info("--once flag set: running single cycle then exiting")
        scheduler._run_cycle()
        logger.info("Single cycle complete. Exiting.")
    else:
        # Normal mode — run continuously until Ctrl+C
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Stock Alert System stopped by user.")


if __name__ == "__main__":
    main()