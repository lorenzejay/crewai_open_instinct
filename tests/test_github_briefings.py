import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
import pytest

from crewai_open_instinct import automation_tools, proactive


def test_overdue_relative_briefing_runs_now_from_original_receipt_time():
    now = datetime.now(timezone.utc)
    db = Mock()
    tool = automation_tools.automation_tools(db, "job", "lease",
        requested_at=(now-timedelta(minutes=20)).isoformat())[0]
    tool.run(name="Review PRs", kind="briefing", instruction="Brief me on PRs to review",
        timezone_name="America/Los_Angeles", delay_minutes=5, github_repo="crewAIInc/crewAI")
    args = db.rpc.call_args.kwargs
    assert args["p_source"] == {"github_repo": "crewAIInc/crewAI"}
    assert datetime.fromisoformat(args["p_schedule"]["at"]) < now
    assert now <= datetime.fromisoformat(args["p_next"]) <= now+timedelta(seconds=5)
    assert args["p_job"] == "job" and args["p_token"] == "lease"


@pytest.mark.parametrize("repo", ["https://github.com/owner/repo", "owner/repo/extra", "../repo", "owner;drop/repo"])
def test_bad_repository_never_reads_accounts(repo):
    db = Mock()
    with pytest.raises(ValueError):
        proactive.github_read(db, job_id="job", lease_token="lease", workspace_id="workspace", repository=repo)
    db.rpc.assert_not_called()


def test_github_reads_fixed_tools_and_distinguishes_personal_requests(monkeypatch):
    def outcome(results):
        return SimpleNamespace(isError=False, structuredContent={"successful": True, "data": {"results": [
            {"tool_slug": slug, "response": {"successful": True, "data": data}} for slug, data in results]}})
    pr = {"number": 1, "title": "Fix bug", "html_url": "https://github.com/owner/repo/pull/1", "user": {"login": "colleague"}}
    protocol = SimpleNamespace(initialize=AsyncMock(), call_tool=AsyncMock(side_effect=[
        outcome([("GITHUB_GET_THE_AUTHENTICATED_USER", {"login": "pilot", "email": "private@example.com"}),
                 ("GITHUB_LIST_PULL_REQUESTS", {"pull_requests": [pr, pr | {"draft": True}, pr | {"user": {"login": "pilot"}}]})]),
        outcome([("GITHUB_FIND_PULL_REQUESTS", {"pull_requests": [pr]})])]))
    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, None
    @asynccontextmanager
    async def session(*args):
        yield protocol
    monkeypatch.setattr(proactive, "streamable_http_client", transport)
    monkeypatch.setattr(proactive, "ClientSession", session)
    result = asyncio.run(proactive._github_read(SimpleNamespace(url="https://backend.composio.dev/mcp/test", headers={}), "owner/repo"))
    assert len(result["pending_personal_reviews"]) == 1
    assert len(result["recent_open_candidates"]) == 1
    assert "private@example.com" not in json.dumps(result)
    calls = protocol.call_tool.call_args_list
    slugs = [t["tool_slug"] for c in calls for t in c.kwargs["arguments"]["tools"]]
    assert slugs == ["GITHUB_GET_THE_AUTHENTICATED_USER", "GITHUB_LIST_PULL_REQUESTS", "GITHUB_FIND_PULL_REQUESTS"]
    assert calls[1].kwargs["arguments"]["tools"][0]["arguments"]["query"] == "user-review-requested:pilot"


def test_github_provider_error_cannot_become_empty_review_list():
    with pytest.raises(RuntimeError):
        proactive.account_result({"successful": False, "error": "unavailable"}, "GITHUB_LIST_PULL_REQUESTS")


def test_scheduled_github_job_uses_its_workspace_and_no_write_tools(monkeypatch):
    reader = Mock(return_value={"pending_personal_reviews": []})
    monkeypatch.setattr(proactive, "github_read", reader)
    monkeypatch.setattr(proactive, "github_briefing", lambda *a, **kw: "No personal review requests.")
    db = Mock()
    proactive.generate(db, job_id="job", lease_token="lease", context={
        "workspace_id": "private-workspace", "automation": {"kind": "briefing",
        "instruction": "Review PRs", "source": {"github_repo": "owner/repo"}}})
    reader.assert_called_once_with(db, job_id="job", lease_token="lease", workspace_id="private-workspace", repository="owner/repo")
    assert db.rpc.call_args.kwargs["p_reply"] == "No personal review requests."


def test_review_assignment_label_comes_from_data_even_if_summary_is_wrong(monkeypatch):
    agent = Mock()
    agent.kickoff.return_value.pydantic = proactive.PRBrief(entries=[proactive.PRSummary(number=1, summary="Fixes SQLite contention.")])
    monkeypatch.setattr(proactive, "Agent", lambda **kw: agent)
    monkeypatch.setattr(proactive, "LLM", lambda **kw: "test-model")
    evidence = {"repository": "owner/repo", "pending_personal_reviews": [
        {"number": 1, "title": "Fix contention", "html_url": "https://github.com/owner/repo/pull/1"}],
        "recent_open_candidates": []}
    reply = proactive.github_briefing(evidence)
    assert "Direct review requests (showing 1 of 1 fetched)" in reply
    assert "Suggested" not in reply and "https://github.com/owner/repo/pull/1" in reply


def test_missing_pr_fields_fail_instead_of_fabricating_empty_review_list(monkeypatch):
    agent = Mock()
    agent.kickoff.return_value.pydantic = proactive.PRBrief(entries=[proactive.PRSummary(number=99, summary="Unknown PR")])
    monkeypatch.setattr(proactive, "Agent", lambda **kw: agent)
    monkeypatch.setattr(proactive, "LLM", lambda **kw: "test-model")
    with pytest.raises(ValueError):
        proactive.github_briefing({"repository": "owner/repo", "pending_personal_reviews": [
            {"number": 1, "title": "PR", "html_url": "https://github.com/owner/repo/pull/1"}], "recent_open_candidates": []})
