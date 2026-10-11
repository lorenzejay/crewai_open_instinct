"""Private Composio sessions; CrewAI executes account tools over hosted MCP."""
import os
from urllib.parse import urlsplit

import httpx
from composio import Composio
from crewai.mcp import MCPServerHTTP
from crewai.mcp.filters import create_static_tool_filter


TOOLKITS = ["gmail", "github"]


def validate_session(session, user_id: str):
    config = session.config
    allowed = getattr(config.toolkits, "enabled", None)
    if config.user_id != user_id or allowed is None or set(allowed) != set(TOOLKITS):
        raise ValueError("Account session identity or toolkit scope mismatch")
    if config.workbench is None or config.workbench.enable is not False:
        raise ValueError("Account session must disable remote code execution")
    if config.connected_accounts:
        raise ValueError("Account session must use its user's own connections")
    target = urlsplit(session.mcp.url)
    if (target.scheme != "https" or target.hostname != "backend.composio.dev"
            or target.port not in (None, 443) or target.username or target.password):
        raise ValueError("Unexpected account MCP destination")


def account_mcps(db, *, job_id: str, lease_token: str, workspace_id: str):
    key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not key:
        return []
    params = {"p_job": job_id, "p_token": lease_token}
    saved = db.rpc("ci_account_session", **params)
    if saved["workspace_id"] != workspace_id:
        raise ValueError("Account workspace mismatch")
    user_id = saved["user_id"]
    with httpx.Client(timeout=20) as transport:
        client = Composio(api_key=key, base_url="https://backend.composio.dev",
                          http_client=transport, timeout=20, max_retries=0, allow_tracking=False,
                          dangerously_allow_auto_upload_download_files=False)
        if saved["session_id"]:
            session = client.use(saved["session_id"], mcp=True)
        else:
            session = client.create(user_id=user_id, toolkits=TOOLKITS,
                                    sandbox={"enable": False},
                                    manage_connections={"enable": True, "wait_for_connections": False},
                                    mcp=True)
            try:
                validate_session(session, user_id)
                winner = db.rpc("ci_account_session", **params, p_session=session.session_id)
                if winner["workspace_id"] != workspace_id or winner["user_id"] != user_id:
                    raise ValueError("Account identity changed during session creation")
            except Exception:
                session.delete()
                raise
            if winner["session_id"] != session.session_id:
                session.delete()
                session = client.use(winner["session_id"], mcp=True)
        validate_session(session, user_id)
        return [MCPServerHTTP(
            url=session.mcp.url, headers=session.mcp.headers, streamable=True,
            cache_tools_list=False,
            tool_filter=create_static_tool_filter(blocked_tool_names=[
                "COMPOSIO_WAIT_FOR_CONNECTIONS", "COMPOSIO_REMOTE_WORKBENCH",
                "COMPOSIO_REMOTE_BASH_TOOL",
            ]),
        )]
