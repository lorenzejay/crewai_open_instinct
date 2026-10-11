"""CrewAI 1.15.23 transport compatibility for Composio's hosted MCP.

CrewAI enters its HTTP transport using asyncio.wait_for(), which creates a
different task. MCP's AnyIO task group must be entered and exited in the same
task; otherwise tool execution fails during disconnect. Keep the connection
timeout while entering the SDK context in the caller's task.
"""
import asyncio
from contextlib import asynccontextmanager

import httpx
from crewai import Agent
from crewai.mcp import MCPServerHTTP
from crewai.mcp.tool_resolver import MCPToolResolver
from crewai.mcp.transports.http import HTTPTransport
from mcp.client.streamable_http import streamable_http_client


class ComposioHTTPTransport(HTTPTransport):
    @asynccontextmanager
    async def _open(self):
        async with httpx.AsyncClient(headers=self.headers, timeout=30) as client:
            async with streamable_http_client(self.url, http_client=client,
                                              terminate_on_close=False) as streams:
                yield streams

    async def connect(self):
        if self.connected:
            return self
        self._transport_context = self._open()
        try:
            # wait_for(__aenter__()) breaks AnyIO's task ownership on teardown.
            async with asyncio.timeout(30):
                read, write, _ = await self._transport_context.__aenter__()
        except BaseException as exc:
            teardown = await self._close_transport_context(exc)
            if teardown is not None:
                self._raise_teardown_failure(teardown)
            raise
        self._set_streams(read, write)
        return self


class ComposioMCPResolver(MCPToolResolver):
    @staticmethod
    def _create_transport(config):
        if isinstance(config, MCPServerHTTP):
            return ComposioHTTPTransport(url=config.url, headers=config.headers,
                                         streamable=config.streamable), "backend.composio.dev"
        return MCPToolResolver._create_transport(config)


class ComposioAgent(Agent):
    def get_mcp_tools(self, mcps):
        self._cleanup_mcp_clients()
        self._mcp_resolver = ComposioMCPResolver(agent=self, logger=self._logger)
        return self._mcp_resolver.resolve(mcps)
