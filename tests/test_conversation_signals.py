import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from crewai_open_instinct import main
from crewai_open_instinct.providers import ProviderError, Sendblue
from crewai_open_instinct.worker import process_job


def provider(monkeypatch, handler):
    monkeypatch.setenv("SENDBLUE_API_KEY", "test-key")
    monkeypatch.setenv("SENDBLUE_API_SECRET", "test-secret")
    delivery = Sendblue()
    delivery.client.close()
    delivery.client = httpx.Client(base_url="https://api.sendblue.com",
                                    transport=httpx.MockTransport(handler))
    return delivery


def test_provider_sends_explicit_line_and_stops_without_duration(monkeypatch):
    requests = []
    def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "OK"})
    delivery = provider(monkeypatch, handler)
    try:
        target = {"line": "+15555550100", "recipient": "+15555550101"}
        delivery.mark_read(**target)
        delivery.typing(**target, state="start")
        delivery.typing(**target, state="stop")
    finally:
        delivery.close()
    assert requests == [
        ("/api/mark-read", {"from_number": target["line"], "number": target["recipient"]}),
        ("/api/send-typing-indicator", {"from_number": target["line"], "number": target["recipient"], "state": "start", "max_duration_ms": 240000}),
        ("/api/send-typing-indicator", {"from_number": target["line"], "number": target["recipient"], "state": "stop"}),
    ]


@pytest.mark.parametrize("status,body", [(422, {"message": "private provider body"}), (200, {"status": "ERROR", "error_message": "private provider body"})])
def test_provider_rejection_is_reported_without_body(monkeypatch, status, body):
    delivery = provider(monkeypatch, lambda request: httpx.Response(status, json=body))
    try:
        for action in [lambda: delivery.mark_read(line="line", recipient="phone"),
                       lambda: delivery.typing(line="line", recipient="phone", state="start")]:
            with pytest.raises(ProviderError) as error:
                action()
            assert "private provider body" not in str(error.value)
    finally:
        delivery.close()


def setup_generation(monkeypatch, fail=False):
    monkeypatch.setenv("SENDBLUE_PHONE_NUMBER", "+15555550100")
    order = []
    db, delivery = Mock(), Mock()
    def rpc(name, **params):
        order.append(name)
        return {"line": "+15555550100", "phone": "+15555550101"} if name == "ci_context" else True
    db.rpc.side_effect = rpc
    delivery.mark_read.side_effect = lambda **kw: order.append("read")
    delivery.typing.side_effect = lambda **kw: order.append(kw["state"])
    flow = Mock(state=SimpleNamespace(reply="Hello"))
    def kickoff(**kwargs):
        order.append("generate")
        if fail:
            raise RuntimeError("generation failed")
    flow.kickoff.side_effect = kickoff
    monkeypatch.setattr(main, "AssistantFlow", lambda: flow)
    return db, delivery, flow, order


def test_read_and_typing_surround_generation(monkeypatch):
    db, delivery, _, order = setup_generation(monkeypatch)
    process_job(db, delivery, {"id": "job", "lease_token": "lease", "status": "running"})
    assert order == ["ci_context", "read", "start", "generate", "ci_reply", "stop"]
    delivery.mark_read.assert_called_once_with(line="+15555550100", recipient="+15555550101")
    delivery.send.assert_not_called()


def test_generation_failure_always_clears_typing(monkeypatch):
    db, delivery, _, order = setup_generation(monkeypatch, fail=True)
    process_job(db, delivery, {"id": "job", "lease_token": "lease", "status": "running"})
    assert order == ["ci_context", "read", "start", "generate", "ci_reply", "stop"]
    assert "couldn't finish" in db.rpc.call_args_list[-1].kwargs["p_reply"]
    delivery.send.assert_not_called()


def test_signal_failures_do_not_fail_reply_or_leak_errors(monkeypatch, caplog):
    db, delivery, _, order = setup_generation(monkeypatch)
    delivery.mark_read.side_effect = TimeoutError("private details")
    delivery.typing.side_effect = ProviderError("private details")
    process_job(db, delivery, {"id": "job", "lease_token": "lease", "status": "running"})
    assert "ci_reply" in order and "ci_error" not in order
    assert delivery.typing.call_count == 2
    assert "private details" not in caplog.text


def test_expired_lease_never_signals_another_contact(monkeypatch):
    db, delivery, flow, _ = setup_generation(monkeypatch)
    db.rpc.side_effect = ProviderError("Inactive job lease")
    with pytest.raises(ProviderError):
        process_job(db, delivery, {"id": "job", "lease_token": "lease", "status": "running"})
    delivery.mark_read.assert_not_called()
    delivery.typing.assert_not_called()
    flow.kickoff.assert_not_called()


def test_wrong_line_never_signals_or_generates(monkeypatch):
    db, delivery, flow, _ = setup_generation(monkeypatch)
    db.rpc.side_effect = None
    db.rpc.return_value = {"line": "+15555550999", "phone": "+15555550101"}
    process_job(db, delivery, {"id": "job", "lease_token": "lease", "status": "running"})
    delivery.mark_read.assert_not_called()
    delivery.typing.assert_not_called()
    flow.kickoff.assert_not_called()
