import httpx

from .config import required


class ProviderError(RuntimeError):
    """Report status codes rather than provider bodies containing sensitive data."""


def json_response(response: httpx.Response, service: str):
    if not response.is_success:
        raise ProviderError(f"{service} returned HTTP {response.status_code}")
    # PostgREST returns HTTP 204 with no body for SQL functions returning void.
    if response.status_code == 204:
        return None
    return response.json()


class Supabase:
    def __init__(self):
        self.client = httpx.Client(
            base_url=required("SUPABASE_URL").rstrip("/"),
            headers={"apikey": required("SUPABASE_SECRET_KEY")}, timeout=20,
        )

    def close(self):
        self.client.close()

    def rpc(self, name: str, **params):
        return json_response(self.client.post(f"/rest/v1/rpc/{name}", json=params), "Supabase")

    def user(self, token: str):
        response = self.client.get("/auth/v1/user", headers={
            "apikey": required("SUPABASE_PUBLISHABLE_KEY"),
            "Authorization": f"Bearer {token}",
        })
        return json_response(response, "Supabase Auth")


class Sendblue:
    def __init__(self):
        # Do not configure transport retries: a timeout may happen after acceptance.
        self.client = httpx.Client(base_url="https://api.sendblue.com", timeout=20, headers={
            "sb-api-key-id": required("SENDBLUE_API_KEY"),
            "sb-api-secret-key": required("SENDBLUE_API_SECRET"),
        })

    def close(self):
        self.client.close()

    def send(self, *, line: str, recipient: str, content: str):
        data = json_response(self.client.post("/api/send-message", json={
            "from_number": line, "number": recipient, "content": content,
        }), "Sendblue")
        if data.get("status") == "ERROR" or not data.get("message_handle"):
            raise ProviderError("Sendblue did not confirm message acceptance")
        return data["message_handle"]

    def mark_read(self, *, line: str, recipient: str):
        data = json_response(self.client.post("/api/mark-read", json={
            "from_number": line, "number": recipient,
        }, timeout=5), "Sendblue read receipt")
        if data.get("status") != "OK":
            raise ProviderError("Sendblue did not accept the read receipt")

    def typing(self, *, line: str, recipient: str, state: str, max_duration_ms: int = 240000):
        if state not in ("start", "stop"):
            raise ValueError("Invalid typing state")
        body = {"from_number": line, "number": recipient, "state": state}
        if state == "start":
            if not 1 <= max_duration_ms <= 300000:
                raise ValueError("Invalid typing duration")
            body["max_duration_ms"] = max_duration_ms
        data = json_response(self.client.post("/api/send-typing-indicator", json=body,
                                             timeout=5), "Sendblue typing indicator")
        if data.get("status") not in ("SENT", "QUEUED", "OK"):
            raise ProviderError("Sendblue did not accept the typing indicator")
