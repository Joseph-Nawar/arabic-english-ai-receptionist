"""Shared pytest configuration."""

from __future__ import annotations

import httplib2
import pytest


@pytest.fixture(autouse=True)
def block_live_calendar_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every automated test fail closed before any real HTTP transport use."""

    def blocked_request(*args: object, **kwargs: object) -> object:
        raise AssertionError("live Calendar network access is forbidden in automated tests")

    monkeypatch.setattr(httplib2.Http, "request", blocked_request)
