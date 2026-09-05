"""Small structured logger shared by the worker and the dashboard."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False
_FMT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_DATEFMT = "%H:%M:%S"


def setup(level: int = logging.INFO, logfile: Path | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(level=level, format=_FMT, datefmt=_DATEFMT, handlers=handlers)
    # These libraries are chatty at INFO and drown out pipeline progress.
    for noisy in ("asyncio", "aiohttp.access", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
