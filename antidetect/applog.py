"""Central logging setup: level from env, rotating UTF-8 file, optional console."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ANTIDETECT_DATA_DIR") or os.path.join(BASE_DIR, "data")
LOG_FILE = os.path.join(DATA_DIR, "app.log")

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _level_from_env() -> int:
    raw = (os.environ.get("ANTIDETECT_LOG_LEVEL") or "INFO").upper()
    return getattr(logging, raw, logging.INFO) if isinstance(raw, str) else logging.INFO


def setup_logging(
    log_path: Optional[str] = None, console: Optional[bool] = None
) -> logging.Logger:
    """Configure the ``antidetect`` logger once; safe to call repeatedly."""
    global _CONFIGURED
    logger = logging.getLogger("antidetect")
    if _CONFIGURED:
        return logger
    logger.setLevel(_level_from_env())
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, _DATEFMT)

    path = log_path or LOG_FILE
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=5,
            encoding="utf-8", delay=True,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    if console is None:
        console = (os.environ.get("ANTIDETECT_LOG_CONSOLE") or "").lower() in (
            "1", "true", "yes", "on",
        )
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    _CONFIGURED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger("antidetect." + name)
