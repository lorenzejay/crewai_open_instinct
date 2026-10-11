"""Load secrets without exposing them in logs or error messages."""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "openai/gpt-6.1-sol"


def load_environment() -> None:
    # This project is self-contained; exported variables override its .env.
    load_dotenv(PROJECT / ".env", override=False)


def llm_options(*, max_tokens: int) -> dict:
    """Use Responses for GPT-6 tool calls; retain existing provider overrides."""
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL)
    options = {"model": model, "timeout": 45, "max_tokens": max_tokens}
    if model.startswith("openai/gpt-6"):
        # GPT-6.1 Sol requires Responses for tools and does not support "none".
        options.update(api="responses", reasoning_effort="low", store=False)
    return options


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("YOUR_"):
        raise ValueError(f"Missing environment variable: {name}")
    return value


def phone(value: str) -> str:
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise ValueError("A phone number must use E.164 format")
    return value


def service_line() -> str:
    return phone(required("SENDBLUE_PHONE_NUMBER"))


def check_configuration(*, worker: bool = False) -> list[str]:
    load_environment()
    names = ["SUPABASE_URL", "SUPABASE_SECRET_KEY",
             "SENDBLUE_API_KEY", "SENDBLUE_API_SECRET", "SENDBLUE_PHONE_NUMBER",
             "SENDBLUE_WEBHOOK_SECRET"]
    if worker:
        names += ["BROWSERBASE_API_KEY"]
        model = os.getenv("LLM_MODEL", DEFAULT_MODEL)
        provider = model.split("/", 1)[0]
        provider_key = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider)
        if provider_key:
            names.append(provider_key)
        else:
            raise ValueError("This prototype supports openai/ and anthropic/ models")
    missing = [name for name in names if not os.getenv(name, "").strip()]
    if not missing:
        service_line()
        if not required("SUPABASE_URL").startswith("https://"):
            raise ValueError("SUPABASE_URL must use HTTPS")
    return missing
