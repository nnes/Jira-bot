"""Centralised logging configuration.

Call setup_logging() once at application startup (app/main.py).
"""
import logging
import logging.handlers
from pathlib import Path

from app.config import settings

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Third-party loggers that are too chatty at DEBUG/INFO — keep at WARNING.
_QUIET_LOGGERS = [
    "httpx",
    "httpcore",
    "urllib3",
    "msrest",
    "asyncio",
    "uvicorn.access",
    "botbuilder",
]

# msrest.serialization warns about unknown fields that the botbuilder-schema SDK
# hasn't modelled yet (e.g. clientActivityID, clientTimestamp sent by Teams /
# Bot Framework Emulator). They are harmless deserialization noise — suppress them.
_MSREST_IGNORED_ATTRS = frozenset({"clientActivityID", "clientTimestamp"})


class _MsrestUnknownAttrFilter(logging.Filter):
    """Drop msrest.serialization warnings for known-harmless unknown attributes."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "is not a known attribute of class" in msg:
            attr = msg.split()[0]
            if attr in _MSREST_IGNORED_ATTRS:
                return False
        return True


def setup_logging() -> None:
    """Configure the root logger from settings.

    - Always adds a StreamHandler (stderr) at the configured level.
    - Always writes to a rotating file logs/agent.log (max 10 MB, 3 backups).
    - In DEBUG mode, app.* loggers are set to DEBUG; third-party loggers stay at WARNING.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any handlers added by basicConfig or previous calls
    root.handlers.clear()

    formatter = logging.Formatter(_LOG_FORMAT)

    # Console handler
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # Rotating file handler — always enabled
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "agent.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Silence noisy third-party loggers regardless of LOG_LEVEL
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Drop specific known-harmless msrest deserialization warnings
    logging.getLogger("msrest.serialization").addFilter(_MsrestUnknownAttrFilter())
