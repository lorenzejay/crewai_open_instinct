import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from crewai_open_instinct.api import ReceiveEvent, create_app, inbound_identity


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "public-test")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "secret-test")
    monkeypatch.setenv("SENDBLUE_API_KEY", "key-test")
    monkeypatch.setenv("SENDBLUE_API_SECRET", "secret-test")
    monkeypatch.setenv("SENDBLUE_PHONE_NUMBER", "+15555550100")
    monkeypatch.setenv("SENDBLUE_WEBHOOK_SECRET", "test-webhook-secret")
    app = create_app()
    db = Mock()
    db.rpc.return_value = "queued"
    app.state.db = db
    # No lifespan: inject the provider at its public boundary to avoid networks.
    return TestClient(app), db


def event(**updates):
    return {"message_handle": "inbound-1", "is_outbound": False,
            "from_number": "+15555550101", "number": "+15555550101",
            "sendblue_number": "+15555550100", "content": "Hi", **updates}


def test_unauthenticated_webhook_never_touches_database(client):
    http, db = client
    assert http.post("/webhooks/sendblue", json=event()).status_code == 401
    db.rpc.assert_not_called()


def test_verified_webhook_commits_before_acknowledgement(client):
    http, db = client
    response = http.post("/webhooks/sendblue", json=event(), headers={"sb-signing-secret": "test-webhook-secret"})
    assert response.json() == {"status": "queued"}
    assert db.rpc.call_args.kwargs["p_sender"] == "+15555550101"
    assert db.rpc.call_args.args == ("ci_receive_text",)
    assert "p_user_id" not in db.rpc.call_args.kwargs
    db.user.assert_not_called()


def test_database_failure_causes_retry(client):
    from crewai_open_instinct.providers import ProviderError
    http, db = client
    db.rpc.side_effect = ProviderError("failed")
    assert http.post("/webhooks/sendblue", json=event(), headers={"sb-signing-secret": "test-webhook-secret"}).status_code == 503


@pytest.mark.parametrize("updates", [
    {"is_outbound": True}, {"group_id": "private-group"},
    {"sendblue_number": "+15555550199"}, {"message_type": "group"},
])
def test_unrelated_events_do_not_enter_agent(client, updates):
    http, db = client
    response = http.post("/webhooks/sendblue", json=event(**updates), headers={"sb-signing-secret": "test-webhook-secret"})
    assert response.json() == {"status": "ignored"}
    db.rpc.assert_not_called()


def test_conflicting_identity_rejected(client):
    http, db = client
    response = http.post("/webhooks/sendblue", json=event(number="+15555550102"), headers={"sb-signing-secret": "test-webhook-secret"})
    assert response.status_code == 400
    db.rpc.assert_not_called()


def test_link_code_is_hashed_before_storage(client):
    http, db = client
    code = "a" * 32
    response = http.post("/webhooks/sendblue", json=event(content="LINK " + code), headers={"sb-signing-secret": "test-webhook-secret"})
    assert response.status_code == 200
    digest = db.rpc.call_args.kwargs["p_link_hash"]
    assert len(digest) == 64 and digest != code


def test_oversized_webhook_rejected(client):
    http, db = client
    response = http.post("/webhooks/sendblue", content="x" * 65537, headers={"sb-signing-secret": "test-webhook-secret"})
    assert response.status_code == 413
    db.rpc.assert_not_called()


def test_phone_link_requires_supabase_session(client):
    http, db = client
    assert http.post("/v1/phone-links").status_code in (401, 403)
    db.rpc.assert_not_called()


def test_phone_link_tenant_comes_from_verified_session(client):
    http, db = client
    owner = "11111111-1111-1111-1111-111111111111"
    db.user.return_value = {"id": owner}
    response = http.post("/v1/phone-links", json={"user_id": "attacker-choice"}, headers={"Authorization": "Bearer session"})
    assert response.status_code == 200
    assert db.rpc.call_args.kwargs["p_user_id"] == owner
    assert response.json()["message"].startswith("LINK ")


def test_public_config_contains_no_backend_credentials(client):
    http, _ = client
    data = http.get("/v1/public-config").json()
    assert set(data) == {"service_number"}
    assert "secret-test" not in json.dumps(data)


def test_text_first_page_requires_no_sign_in(client):
    http, _ = client
    assert http.get("/test").status_code == 200
    page = http.get("/test").text
    assert "Text your assistant" in page
    assert 'type="password"' not in page
    assert 'id="auth-form"' not in page


def test_composio_route_unconfigured_fails_closed(client, monkeypatch):
    monkeypatch.delenv("COMPOSIO_WEBHOOK_SECRET", raising=False)
    http, db = client
    assert http.post("/webhooks/composio", json={}).status_code == 503
    db.rpc.assert_not_called()


def test_composio_route_bad_signature_never_queues(client, monkeypatch):
    monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key")
    http, db = client
    assert http.post("/webhooks/composio", json={}).status_code == 401
    db.rpc.assert_not_called()


def test_composio_route_verifies_before_database_and_commits_before_ack(client, monkeypatch):
    from crewai_open_instinct import account_events
    monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key")
    params = {"p_event": "event", "p_trigger": "ti", "p_account": "ca", "p_user": "owner", "p_slug": "GMAIL_NEW_GMAIL_MESSAGE", "p_payload": {}}
    verify = Mock(return_value=params)
    monkeypatch.setattr(account_events, "verified_event", verify)
    http, db = client
    assert http.post("/webhooks/composio", json={}).json() == {"status": "queued"}
    verify.assert_called_once()
    db.rpc.assert_called_once_with("ci_account_event", **params)


def test_composio_route_oversized_body_rejected(client, monkeypatch):
    monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key")
    http, db = client
    assert http.post("/webhooks/composio", content="x"*65537).status_code == 413
    db.rpc.assert_not_called()
