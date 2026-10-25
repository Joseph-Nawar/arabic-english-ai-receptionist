from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.unit


def test_sync_httpx_network_transport_is_blocked_in_automated_tests() -> None:
    with (
        httpx.Client() as client,
        pytest.raises(
            AssertionError, match="live HTTP network access is forbidden in automated tests"
        ),
    ):
        client.get("http://127.0.0.1:9/blocked")


@pytest.mark.asyncio
async def test_async_httpx_network_transport_is_blocked_in_automated_tests() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            AssertionError, match="live HTTP network access is forbidden in automated tests"
        ):
            await client.get("http://127.0.0.1:9/blocked")
