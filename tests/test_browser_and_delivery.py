from unittest.mock import Mock

import pytest

from crewai_open_instinct.browser import public_url
from crewai_open_instinct.worker import process_job


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:pass@example.com", "http://localhost", "https://127.0.0.1", "https://example.com:9000"])
def test_private_browser_targets_rejected(url):
    with pytest.raises((ValueError, OSError)):
        public_url(url)


def test_failed_delivery_is_not_automatically_retried(monkeypatch):
    monkeypatch.setenv("SENDBLUE_PHONE_NUMBER", "+15555550100")
    db, delivery = Mock(), Mock()
    db.rpc.return_value = {"line": "+15555550100", "phone": "+15555550101"}
    delivery.send.side_effect = TimeoutError("message might have been accepted")
    process_job(db, delivery, {"id": "job", "lease_token": "token", "status": "sending", "reply": "Hi"})
    delivery.send.assert_called_once()
    db.rpc.assert_called_with("ci_error", p_job="job", p_token="token", p_sending=True, p_code="TimeoutError")


def test_persisted_customer_line_cannot_be_replaced(monkeypatch):
    monkeypatch.setenv("SENDBLUE_PHONE_NUMBER", "+15555550100")
    db, delivery = Mock(), Mock()
    db.rpc.return_value = {"line": "+15555550199", "phone": "+15555550101"}
    process_job(db, delivery, {"id": "job", "lease_token": "token", "status": "sending", "reply": "Hi"})
    delivery.send.assert_not_called()
