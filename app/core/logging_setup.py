"""Console logging plus rotating files in app/logs/. Two files: app.log has everything, errors.log only warnings and above, 
so a failed run can be diagnosed without scrolling past thousands of INFO lines."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import config

FMT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"
DATEFMT = "%H:%M:%S"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FMT, DATEFMT))
    root.addHandler(console)

    full = RotatingFileHandler(config.LOG_DIR / "app.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    full.setFormatter(logging.Formatter(FMT, DATEFMT))
    root.addHandler(full)

    errors = RotatingFileHandler(config.LOG_DIR / "errors.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    errors.setFormatter(logging.Formatter(FMT, DATEFMT))
    root.addHandler(errors)

    # httpx logs every Ollama call at INFO, which drowns out our own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
