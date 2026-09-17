"""Console logging setup for the CLI.

A single helper configures the 'radiofinder' logger with a compact, timestamped
format. Records are written via tqdm.write when a progress bar is active so they
don't corrupt the bar.
"""
from __future__ import annotations

import logging

from tqdm import tqdm

LOGGER_NAME = "radiofinder"


class _TqdmHandler(logging.Handler):
    """Emit log records through tqdm.write so they don't break a live bar."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record))
        except Exception:  # pragma: no cover - logging must never crash the app
            self.handleError(record)


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the package logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = _TqdmHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
