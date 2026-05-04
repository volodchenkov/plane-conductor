"""Smoke tests for logging configuration."""

from __future__ import annotations

import pytest

from plane_conductor.logging_config import configure_logging, get_logger


@pytest.mark.parametrize("fmt", ["pretty", "json"])
@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
def test_configure_logging_does_not_raise(fmt: str, level: str) -> None:
    configure_logging(level=level, fmt=fmt)
    log = get_logger("test")
    log.info("hello", k="v")  # should not raise


def test_get_logger_named_and_default() -> None:
    configure_logging(level="WARNING", fmt="pretty")
    a = get_logger()
    b = get_logger("mod")
    assert a is not None
    assert b is not None
