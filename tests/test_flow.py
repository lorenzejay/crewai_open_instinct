from types import SimpleNamespace
from unittest.mock import Mock

from crewai_open_instinct import main


def test_flow_uses_only_current_job_context(monkeypatch):
    db = Mock()
    db.rpc.side_effect = lambda name, **kw: ({"workspace_id": "workspace", "content": "remember my preference", "history": [], "memories": []}
        if name == "ci_context" else "remember my preference")
    monkeypatch.setattr(main, "Supabase", lambda: db)
    monkeypatch.setattr(main, "LLM", lambda **kwargs: "test-model")
    monkeypatch.setattr(main, "account_mcps", lambda *args, **kwargs: [])
    agent = Mock()
    agent.kickoff.return_value = SimpleNamespace(raw="Preference saved.")
    created = {}

    def agent_factory(**kwargs):
        created.update(kwargs)
        return agent

    monkeypatch.setattr(main, "Agent", agent_factory)
    flow = main.AssistantFlow()
    flow.kickoff(inputs={"job_id": "authorized-job", "lease_token": "lease"})
    assert flow.state.reply == "Preference saved."
    db.rpc.assert_called_with("ci_coalesce_user_turn", p_job="authorized-job", p_token="lease")
    tools = {t.name: t for t in created["tools"]}
    db.rpc.return_value = True
    db.rpc.side_effect = None
    assert tools["remember_note"].run(name="drink", content="coffee") == "Saved."
    db.rpc.assert_called_with("ci_save_memory", p_job="authorized-job", p_token="lease", p_name="drink", p_content="coffee")
    assert created["allow_delegation"] is False and created["cache"] is False


def test_schedule_confirmation_requires_database_evidence(monkeypatch):
    db = Mock()
    request = "My timezone is America/Los_Angeles. In 5 minutes give me a brief of PRs in https://github.com/crewAIInc/crewAI"
    db.rpc.side_effect = lambda name, **kw: {"workspace_id": "workspace", "content": request} if name == "ci_context" else request
    monkeypatch.setattr(main, "Supabase", lambda: db)
    monkeypatch.setattr(main, "LLM", lambda **kw: "test-model")
    account = Mock()
    monkeypatch.setattr(main, "account_mcps", account)
    agent = Mock()
    agent.kickoff.return_value = SimpleNamespace(raw="In 5 minutes, I will send you a briefing.")
    factory = Mock(return_value=agent)
    monkeypatch.setattr(main, "SchedulingAgent", factory)
    monkeypatch.setattr(main, "plan_schedule", lambda *a: main.SchedulePlan(action="other"))
    flow = main.AssistantFlow()
    flow.kickoff(inputs={"job_id": "job", "lease_token": "lease"})
    assert flow.state.reply.startswith("No new task was saved")
    account.assert_not_called()
    assert factory.call_args.kwargs["mcps"] is None
    assert {t.name for t in factory.call_args.kwargs["tools"]} == {"create_automation", "manage_automations", "watch_account"}


def test_structured_schedule_is_saved_by_code_without_model_tool_choice(monkeypatch):
    request = "In 5 minutes give me a PR brief"
    db = Mock()
    def rpc(name, **kw):
        if name == "ci_context": return {"workspace_id": "workspace", "content": request}
        if name == "ci_coalesce_user_turn": return request
        assert name == "ci_put_automation"
        return {"name": kw["p_name"], "timezone": kw["p_timezone"], "next_run_at": kw["p_next"]}
    db.rpc.side_effect = rpc
    monkeypatch.setattr(main, "Supabase", lambda: db)
    monkeypatch.setattr(main, "plan_schedule", lambda *a: main.SchedulePlan(action="create", name="PR brief",
        kind="briefing", instruction="Brief me on PRs", timezone_name="America/Los_Angeles",
        delay_minutes=5, github_repo="owner/repo"))
    factory = Mock()
    monkeypatch.setattr(main, "SchedulingAgent", factory)
    flow = main.AssistantFlow()
    flow.kickoff(inputs={"job_id": "job", "lease_token": "lease"})
    assert flow.state.reply.startswith('Saved "PR brief" for')
    assert db.rpc.call_args.kwargs["p_source"] == {"github_repo": "owner/repo"}
    factory.assert_not_called()


def test_confirmation_uses_actual_saved_time_instead_of_model_promise():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    reply = main.saved_confirmation([{"name": "Review PRs", "timezone": "America/Los_Angeles",
        "next_run_at": (now+timedelta(seconds=1)).isoformat()}], (now-timedelta(minutes=20)).isoformat())
    assert "delayed" in reply and "due now" in reply
    assert "5 minutes" not in reply


def test_attachment_only_turn_does_not_call_model(monkeypatch):
    db = Mock()
    db.rpc.side_effect = lambda name, **kw: {"content": "", "media_url": "https://example.com/file"} if name == "ci_context" else ""
    monkeypatch.setattr(main, "Supabase", lambda: db)
    factory = Mock()
    monkeypatch.setattr(main, "Agent", factory)
    flow = main.AssistantFlow()
    flow.kickoff(inputs={"job_id": "job", "lease_token": "lease"})
    assert "as text" in flow.state.reply
    factory.assert_not_called()
