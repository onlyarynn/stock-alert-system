Stock Market Alert System

An automated Python application that monitors Nifty 50 and Sensex in real time and sends email alerts when significant price movements are detected.

Features


Live price monitoring via Yahoo Finance (≈15-min delayed, free tier)
Two alert levels — NORMAL and CRITICAL — with independent cooldowns
Gmail email alerts with formatted subject lines and detailed body
SQLite persistence — stores all alert history and price baselines
Market hours enforcement — only runs Mon–Fri, 9:15am–3:30pm IST
Retry logic — exponential back-off on fetch failures
Dry-run mode — test the full pipeline without sending real emails
CLI flags — override threshold, cooldown, log level at runtime

Project Structure:

stock-alert/
├── src/
│   ├── config.py        # Settings loaded from .env (Pydantic)
│   ├── fetcher.py       # Yahoo Finance price fetching with retry
│   ├── analyzer.py      # Price comparison and alert classification
│   ├── notifier.py      # Gmail SMTP email dispatch
│   ├── scheduler.py     # APScheduler polling loop + market hours
│   └── database.py      # SQLAlchemy ORM (SQLite)
├── tests/
│   └── test_analyzer.py # Unit tests for analyzer logic
├── data/
│   └── alerts.db        # SQLite database (auto-created)
├── logs/
│   └── stock_alert.log  # Rotating log file (auto-created)
├── main.py              # Entry point + CLI
├── .env                 # Your credentials and settings (see below)
└── requirements.txt