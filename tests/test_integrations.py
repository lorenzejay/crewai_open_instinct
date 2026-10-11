from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from crewai_open_instinct import integrations, main


def session(user="workspace", scope=None):
    return SimpleNamespace(
        session_id="session-a", delete=Mock(),
        config=SimpleNamespace(user_id=user,
            toolkits=SimpleNamespace(enabled=scope or ["gmail", "github"]),
            workbench=SimpleNamespace(enable=False), connected_accounts=None),
        mcp=SimpleNamespace(url="https://backend.composio.dev/session/mcp",
                            headers={"x-api-key": "private-session-key"}),
    )


def setup(monkeypatch, stored=None):
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-project-key")
    db = Mock()
    saved = {"workspace_id": "workspace", "user_id": "workspace", "session_id": stored}
    db.rpc.side_effect = [saved, {**saved, "session_id": "session-a"}]
    client = Mock()
    client.create.return_value = session()
    client.use.return_value = session()
    monkeypatch.setattr(integrations, "Composio", lambda **kwargs: client)
    return db, client


def get(db):
    return integrations.account_mcps(db, job_id="job", lease_token="lease", workspace_id="workspace")


def test_optional_key_does_not_touch_accounts(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    db = Mock()
    assert get(db) == []
    db.rpc.assert_not_called()


def test_creates_scoped_session_and_passes_mcp_auth(monkeypatch):
    db, client = setup(monkeypatch)
    servers = get(db)
    client.create.assert_called_once_with(user_id="workspace", toolkits=["gmail", "github"],
        sandbox={"enable": False}, manage_connections={"enable": True, "wait_for_connections": False}, mcp=True)
    db.rpc.assert_called_with("ci_account_session", p_job="job", p_token="lease", p_session="session-a")
    assert servers[0].headers == {"x-api-key": "private-session-key"}
    assert servers[0].streamable and not servers[0].cache_tools_list
    assert not servers[0].tool_filter({"name": "COMPOSIO_REMOTE_BASH_TOOL"})
    assert not servers[0].tool_filter({"name": "COMPOSIO_WAIT_FOR_CONNECTIONS"})
    assert servers[0].tool_filter({"name": "COMPOSIO_MANAGE_CONNECTIONS"})


def test_restores_persisted_session_without_new_auth(monkeypatch):
    db, client = setup(monkeypatch, "session-a")
    get(db)
    client.use.assert_called_once_with("session-a", mcp=True)
    client.create.assert_not_called()


def test_existing_identity_is_server_bound(monkeypatch):
    db, client = setup(monkeypatch)
    db.rpc.side_effect = [{"workspace_id": "workspace", "user_id": "pilot-owner", "session_id": "session-a"}]
    client.use.return_value = session("pilot-owner")
    assert get(db)


@pytest.mark.parametrize("failure", ["workspace", "owner", "toolkits", "sandbox", "overrides", "host"])
def test_rejects_cross_tenant_or_unrestricted_sessions(monkeypatch, failure):
    db, client = setup(monkeypatch, "session-a")
    s = client.use.return_value
    if failure == "workspace":
        db.rpc.side_effect = [{"workspace_id": "other", "user_id": "workspace", "session_id": "session-a"}]
    elif failure == "owner":
        s.config.user_id = "other"
    elif failure == "toolkits":
        s.config.toolkits.enabled.append("canva")
    elif failure == "sandbox":
        s.config.workbench.enable = True
    elif failure == "overrides":
        s.config.connected_accounts = {"gmail": ["other-account"]}
    elif failure == "host":
        s.mcp.url = "https://example.com/mcp"
    with pytest.raises(ValueError):
        get(db)


def test_lost_lease_removes_uncommitted_session(monkeypatch):
    db, client = setup(monkeypatch)
    db.rpc.side_effect = [{"workspace_id": "workspace", "user_id": "workspace", "session_id": None}, RuntimeError("Inactive lease")]
    with pytest.raises(RuntimeError):
        get(db)
    client.create.return_value.delete.assert_called_once()


def test_concurrent_creation_uses_stored_winner(monkeypatch):
    db, client = setup(monkeypatch)
    db.rpc.side_effect = [{"workspace_id": "workspace", "user_id": "workspace", "session_id": None},
                          {"workspace_id": "workspace", "user_id": "workspace", "session_id": "winner"}]
    get(db)
    client.create.return_value.delete.assert_called_once()
    client.use.assert_called_once_with("winner", mcp=True)


@pytest.mark.parametrize("available", [True, False])
def test_flow_exposes_mcp_and_handles_provider_failure(monkeypatch, available):
    db = Mock()
    db.rpc.side_effect = lambda name, **kw: {"workspace_id": "workspace", "content": "Check GitHub", "history": [], "memories": []} if name == "ci_context" else "Check GitHub"
    monkeypatch.setattr(main, "Supabase", lambda: db)
    monkeypatch.setattr(main, "LLM", lambda **kwargs: "test-model")
    mcps = ["scoped-mcp"]
    lookup = Mock(return_value=mcps) if available else Mock(side_effect=RuntimeError("secret-provider-body"))
    monkeypatch.setattr(main, "account_mcps", lookup)
    agent = Mock()
    agent.kickoff.return_value = SimpleNamespace(raw="Account response.")
    factory = Mock(return_value=agent)
    monkeypatch.setattr(main, "Agent", factory)
    main.AssistantFlow().kickoff(inputs={"job_id": "job", "lease_token": "lease"})
    lookup.assert_called_once_with(db, job_id="job", lease_token="lease", workspace_id="workspace")
    assert factory.call_args.kwargs["mcps"] == (mcps if available else None)
    prompt = agent.kickoff.call_args.args[0]
    assert "secret-provider-body" not in prompt
    assert ("return the hosted Connect Link immediately" if available else "Account tools are unavailable") in prompt
