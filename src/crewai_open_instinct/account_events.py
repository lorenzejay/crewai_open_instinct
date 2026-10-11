"""Account subscription management and authenticated, tenant-scoped event routing."""
import os

import httpx
from composio import Composio

from .config import required

TRIGGERS = {"gmail": "GMAIL_NEW_GMAIL_MESSAGE", "github": "GITHUB_COMMIT_EVENT"}


def create_watch(db, *, job_id, lease_token, name, toolkit, instruction, trigger_config):
    required("COMPOSIO_WEBHOOK_SECRET")
    if toolkit not in TRIGGERS or not isinstance(trigger_config, dict):
        raise ValueError("Only Gmail message and GitHub commit watches are supported")
    if not 1 <= len(name) <= 100 or not 1 <= len(instruction) <= 2000:
        raise ValueError("Invalid name or instruction")
    if toolkit == "github" and (set(trigger_config) != {"owner", "repo"}
            or any(not isinstance(x, str) or not 1 <= len(x) <= 100 or "/" in x
                   for x in trigger_config.values())):
        raise ValueError("GitHub requires owner and repo")
    if toolkit == "gmail" and trigger_config:
        raise ValueError("This Gmail watch uses the default new-message trigger")
    # An inbound lease check precedes any external subscription side effect.
    db.rpc("ci_user_job", p_job=job_id, p_token=lease_token)
    identity = db.rpc("ci_account_session", p_job=job_id, p_token=lease_token)
    with httpx.Client(timeout=20) as transport:
        client = Composio(api_key=required("COMPOSIO_API_KEY"), base_url="https://backend.composio.dev",
            http_client=transport, timeout=20, max_retries=0, allow_tracking=False,
            dangerously_allow_auto_upload_download_files=False)
        accounts = client.connected_accounts.list(user_ids=[identity["user_id"]],
            toolkit_slugs=[toolkit], statuses=["ACTIVE"], limit=100).items
        matches = [a for a in accounts if a.user_id == identity["user_id"]
                   and a.status == "ACTIVE" and a.toolkit.slug == toolkit]
        if len(matches) != 1:
            raise ValueError("Connect exactly one active account for this toolkit first")
        account = matches[0]
        trigger = client.triggers.create(slug=TRIGGERS[toolkit], user_id=identity["user_id"],
            connected_account_id=account.id, trigger_config=trigger_config)
        return db.rpc("ci_bind_watch", p_job=job_id, p_token=lease_token, p_name=name,
            p_instruction=instruction, p_trigger=trigger.trigger_id, p_account=account.id,
            p_user=identity["user_id"], p_slug=TRIGGERS[toolkit], p_config=trigger_config)


def verified_event(body: bytes, headers) -> dict:
    # SDK performs HMAC verification and a five-minute replay-age check before parsing.
    client = Composio(api_key=required("COMPOSIO_API_KEY"), allow_tracking=False)
    result = client.triggers.parse(body=body, headers=headers,
        verify_secret=required("COMPOSIO_WEBHOOK_SECRET"), tolerance=300)
    event = result["payload"]
    metadata = event.get("metadata", {})
    account = metadata.get("connected_account", {})
    # Legacy V1 events lack a user identity; reject rather than infer a tenant.
    user = event.get("user_id") or account.get("user_id")
    if not user or (account.get("user_id") and account["user_id"] != user):
        raise ValueError("Missing or conflicting event owner")
    trigger = metadata.get("id") or event.get("id")
    account_id = account.get("id")
    slug = event.get("trigger_slug")
    if not trigger or not account_id or slug not in TRIGGERS.values():
        raise ValueError("Unsupported account event")
    return {"p_event": headers.get("webhook-id", ""), "p_trigger": trigger,
            "p_account": account_id, "p_user": user, "p_slug": slug,
            "p_payload": event.get("payload", {})}
