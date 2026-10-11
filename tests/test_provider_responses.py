import httpx
import pytest
from crewai_open_instinct.providers import Supabase, ProviderError, json_response
from crewai_open_instinct import schedules


def test_void_heartbeat_does_not_prevent_claim_or_scheduler_dispatch(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-key")
    paths = []
    def respond(request):
        paths.append(request.url.path)
        if request.url.path.endswith("heartbeat"):
            return httpx.Response(204)
        return httpx.Response(200, json=[])
    db = Supabase()
    db.client.close()
    db.client = httpx.Client(base_url="https://example.supabase.co", transport=httpx.MockTransport(respond))
    try:
        assert db.rpc("ci_worker_heartbeat") is None
        assert db.rpc("ci_claim") == []
        assert schedules.scheduler_tick(db) == 0
        assert paths == ["/rest/v1/rpc/ci_worker_heartbeat", "/rest/v1/rpc/ci_claim",
                         "/rest/v1/rpc/ci_due_automations", "/rest/v1/rpc/ci_scheduler_heartbeat"]
    finally:
        db.close()


def test_invalid_nonempty_json_and_http_failure_still_fail():
    with pytest.raises(ValueError):
        json_response(httpx.Response(200, content=b"invalid"), "Supabase")
    with pytest.raises(ProviderError):
        json_response(httpx.Response(503), "Supabase")
