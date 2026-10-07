"""Logging setup shared by the API and the background worker code."""

import logging

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Send application logs to stderr in one consistent format."""
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    logging.getLogger("app").setLevel(level.upper())
