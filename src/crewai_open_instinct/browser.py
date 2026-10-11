"""Fresh Browserbase sessions for bounded public-page research."""
import ipaddress
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

from .config import required
from .providers import json_response


def public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs without credentials are supported")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Unsupported URL port")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Private network URLs are not supported")
    return url


def _read(url: str) -> str:
    from playwright.sync_api import sync_playwright
    public_url(url)
    with httpx.Client(base_url="https://api.browserbase.com", timeout=20,
                      headers={"x-bb-api-key": required("BROWSERBASE_API_KEY")}) as client:
        project_id = os.getenv("BROWSERBASE_PROJECT_ID")
        body = {"timeout": 90}
        if project_id:
            body["projectId"] = project_id
        session = json_response(client.post("/v1/sessions", json=body), "Browserbase")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(session["connectUrl"], timeout=20000)
                try:
                    context = browser.contexts[0]
                    verified_hosts = set()

                    def guard(route):
                        request = route.request
                        try:
                            if request.method not in ("GET", "HEAD"):
                                return route.abort()
                            host = urlsplit(request.url).netloc
                            if host not in verified_hosts:
                                public_url(request.url)
                                verified_hosts.add(host)
                            route.continue_()
                        except (ValueError, OSError):
                            route.abort()

                    context.route("**/*", guard)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    return "Source: " + page.url + "\nTitle: " + page.title() + "\n" + page.locator("body").inner_text(timeout=10000)[:12000]
                finally:
                    browser.close()
        finally:
            release = {"status": "REQUEST_RELEASE"}
            if project_id:
                release["projectId"] = project_id
            json_response(client.post(f"/v1/sessions/{session['id']}", json=release), "Browserbase")


def read_page(url: str) -> str:
    # Flow runs inside asyncio. Keep Playwright's sync API on its own thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_read, url).result()
