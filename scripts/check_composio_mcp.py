"""Create a temporary session and verify hosted MCP discovery; no account data reads."""
import asyncio
import logging
from uuid import uuid4

from composio import Composio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from crewai_open_instinct.config import load_environment, required
from crewai_open_instinct.integrations import TOOLKITS, validate_session


async def discover(session):
    async with httpx.AsyncClient(headers=session.mcp.headers, timeout=30) as transport:
        async with streamable_http_client(session.mcp.url, http_client=transport) as (read, write, _):
            async with ClientSession(read, write) as protocol:
                await protocol.initialize()
                tools = (await protocol.list_tools()).tools
                names = {t.name for t in tools}
                assert "COMPOSIO_SEARCH_TOOLS" in names
                assert "COMPOSIO_MANAGE_CONNECTIONS" in names
                assert not names & {"COMPOSIO_REMOTE_WORKBENCH", "COMPOSIO_REMOTE_BASH_TOOL", "COMPOSIO_WAIT_FOR_CONNECTIONS"}
                print(f"OK hosted MCP: {len(tools)} tools discovered; Gmail/GitHub scope verified.")


def run():
    load_environment()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.ERROR)
    client = Composio(api_key=required("COMPOSIO_API_KEY"), timeout=20, max_retries=0, allow_tracking=False)
    session = None
    try:
        user_id = "mcp-check-" + str(uuid4())
        session = client.create(user_id=user_id, toolkits=TOOLKITS, sandbox={"enable": False},
            manage_connections={"enable": True, "wait_for_connections": False}, mcp=True)
        validate_session(session, user_id)
        validate_session(client.use(session.session_id, mcp=True), user_id)
        asyncio.run(discover(session))
    except Exception as exc:
        print(f"FAIL Composio MCP ({type(exc).__name__}).")
        if getattr(exc, "status_code", None) == 403:
            print("API key needs Session management write and Session tool execution permissions.")
        raise SystemExit(1) from None
    finally:
        if session:
            session.delete()


if __name__ == "__main__":
    run()
