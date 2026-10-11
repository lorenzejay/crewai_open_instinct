import hashlib
import hmac
import os
import re
import secrets
from contextlib import asynccontextmanager
from uuid import UUID
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import check_configuration, phone, required, service_line
from .providers import ProviderError, Supabase


class ReceiveEvent(BaseModel):
    message_handle: str = Field(min_length=1, max_length=200)
    is_outbound: bool
    from_number: str = ""
    number: str = ""
    sendblue_number: str = ""
    to_number: str = ""
    content: str = Field(default="", max_length=16000)
    media_url: str = Field(default="", max_length=2048)
    group_id: str | None = None
    message_type: str = "message"


def inbound_identity(event: ReceiveEvent, expected_line: str):
    if event.is_outbound or event.group_id or event.message_type != "message":
        return None
    # Documented sendblue_number identifies the receiving line. Never use
    # the sender's number alone to choose a workspace.
    line = phone(event.sendblue_number or event.to_number)
    sender = phone(event.from_number or event.number)
    if line != expected_line:
        return None
    if event.from_number and event.number and event.from_number != event.number:
        raise ValueError("Conflicting sender identity")
    return line, sender


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = check_configuration()
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    app.state.db = Supabase()
    try:
        yield
    finally:
        app.state.db.close()


def create_app():
    app = FastAPI(title="CrewAI iMessage", lifespan=lifespan)
    from .config import load_environment
    load_environment()
    origins = [x.strip() for x in os.getenv("APP_ORIGINS", "http://localhost:3000").split(",") if x.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
    bearer = HTTPBearer()
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/test")

    @app.get("/test", include_in_schema=False)
    def test_page():
        return FileResponse(static / "onboarding.html", headers={"Cache-Control": "no-store"})

    @app.get("/v1/public-config")
    def public_config():
        return {"service_number": service_line()}

    def current_user(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer)):
        try:
            user = request.app.state.db.user(credentials.credentials)
            return str(UUID(user["id"]))
        except (ProviderError, KeyError, ValueError):
            raise HTTPException(401, "Invalid Supabase session") from None

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/phone-links")
    def phone_link(request: Request, user_id: str = Depends(current_user)):
        # The sender proves possession by messaging a random, one-time code.
        # No phone claimed in an API body can select another user's workspace.
        code = secrets.token_hex(16)
        request.app.state.db.rpc("ci_create_link", p_user_id=user_id,
                                p_hash=hashlib.sha256(code.encode()).hexdigest())
        return {"send_to": service_line(), "message": "LINK " + code, "expires_in_seconds": 600}

    @app.get("/v1/workspace")
    def workspace(request: Request, user_id: str = Depends(current_user)):
        return request.app.state.db.rpc("ci_workspace", p_user_id=user_id)

    @app.post("/webhooks/sendblue")
    async def receive(request: Request):
        supplied = request.headers.get("sb-signing-secret", "")
        if not hmac.compare_digest(supplied.encode(), required("SENDBLUE_WEBHOOK_SECRET").encode()):
            raise HTTPException(401, "Invalid webhook secret")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Webhook too large")
        try:
            event = ReceiveEvent.model_validate_json(bytes(body))
            identity = inbound_identity(event, service_line())
        except ValueError:
            raise HTTPException(400, "Invalid Sendblue event") from None
        if identity is None:
            return {"status": "ignored"}
        line, sender = identity
        match = re.fullmatch(r"LINK ([0-9a-f]{32})", event.content.strip())
        link_hash = hashlib.sha256(match.group(1).encode()).hexdigest() if match else None
        # Sync RPC goes to the threadpool so provider I/O doesn't block the API loop.
        from starlette.concurrency import run_in_threadpool
        try:
            result = await run_in_threadpool(request.app.state.db.rpc, "ci_receive_text",
                p_line=line, p_sender=sender, p_handle=event.message_handle,
                p_content=event.content, p_media_url=event.media_url, p_link_hash=link_hash)
        except (ProviderError, httpx.HTTPError):
            # No acknowledgement before the event commits; Sendblue can retry.
            raise HTTPException(503, "Message storage temporarily unavailable") from None
        return {"status": result}

    @app.post("/webhooks/composio")
    async def account_event(request: Request):
        if not os.getenv("COMPOSIO_WEBHOOK_SECRET") or not os.getenv("COMPOSIO_API_KEY"):
            raise HTTPException(503, "Account events are not configured")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Webhook too large")
        from .account_events import verified_event
        from starlette.concurrency import run_in_threadpool
        try:
            params = await run_in_threadpool(verified_event, bytes(body), request.headers)
        except Exception:
            raise HTTPException(401, "Invalid account event") from None
        try:
            result = await run_in_threadpool(request.app.state.db.rpc, "ci_account_event", **params)
        except (ProviderError, httpx.HTTPError):
            raise HTTPException(503, "Event storage temporarily unavailable") from None
        return {"status": result}

    return app


app = create_app()


def serve():
    import uvicorn
    uvicorn.run("crewai_open_instinct.api:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
