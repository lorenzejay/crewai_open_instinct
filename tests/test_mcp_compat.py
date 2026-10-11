import asyncio
from contextlib import asynccontextmanager

import pytest

from crewai.mcp import MCPServerHTTP
from crewai_open_instinct import mcp_compat


def test_transport_enters_and_exits_sdk_in_same_task(monkeypatch):
    entries = []

    @asynccontextmanager
    async def fake_sdk(url, **kwargs):
        owner = asyncio.current_task()
        entries.append(owner)
        assert kwargs["terminate_on_close"] is False
        try:
            yield "read-stream", "write-stream", lambda: None
        finally:
            assert asyncio.current_task() is owner

    monkeypatch.setattr(mcp_compat, "streamable_http_client", fake_sdk)

    async def run():
        transport = mcp_compat.ComposioHTTPTransport(url="https://backend.composio.dev/mcp")
        await transport.connect()
        assert transport.connected
        await transport.connect()
        assert len(entries) == 1
        await transport.disconnect()
        assert not transport.connected
        await transport.disconnect()

    asyncio.run(run())


def test_transport_failure_is_not_swallowed(monkeypatch):
    @asynccontextmanager
    async def refused(url, **kwargs):
        raise ConnectionError("Server unavailable")
        yield

    monkeypatch.setattr(mcp_compat, "streamable_http_client", refused)

    async def run():
        transport = mcp_compat.ComposioHTTPTransport(url="https://backend.composio.dev/mcp")
        with pytest.raises(ConnectionError):
            await transport.connect()
        assert not transport.connected

    asyncio.run(run())


def test_resolver_preserves_private_auth_headers():
    config = MCPServerHTTP(url="https://backend.composio.dev/mcp", headers={"x-api-key": "private"})
    transport, _ = mcp_compat.ComposioMCPResolver._create_transport(config)
    assert isinstance(transport, mcp_compat.ComposioHTTPTransport)
    assert transport.headers == config.headers
