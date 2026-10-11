"""Read-only provider checks; no messages, inference or browser sessions."""
import argparse
import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from .config import check_configuration, required


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="Make read-only authentication checks")
    args = parser.parse_args()
    missing = check_configuration(worker=True)
    for name in missing:
        print(f"MISSING {name}")
    if args.online:
        base = required("SUPABASE_URL").rstrip("/")
        checks = [
            ("Supabase backend", base + "/storage/v1/bucket", {"apikey": required("SUPABASE_SECRET_KEY")}),
            ("Sendblue", "https://api.sendblue.com/api/account/webhooks", {
                "sb-api-key-id": required("SENDBLUE_API_KEY"), "sb-api-secret-key": required("SENDBLUE_API_SECRET")}),
            ("Browserbase", "https://api.browserbase.com/v1/projects", {"x-bb-api-key": required("BROWSERBASE_API_KEY")}),
        ]

        if os.getenv("SUPABASE_PUBLISHABLE_KEY"):
            checks.append(("Supabase public (optional)", base + "/auth/v1/settings", {"apikey": required("SUPABASE_PUBLISHABLE_KEY")}))

        def check(item):
            name, url, headers = item
            try:
                result = httpx.get(url, headers=headers, timeout=15)
                return name, result.is_success, str(result.status_code)
            except httpx.HTTPError:
                return name, False, "connection failed"

        failed = False
        with ThreadPoolExecutor(max_workers=4) as pool:
            for name, ok, status in pool.map(check, checks):
                print(f"{'OK' if ok else 'FAIL'} {name}: {status}")
                failed |= not ok
        if failed:
            raise SystemExit(1)
    if missing:
        raise SystemExit(1)
    print("Configuration ready")


if __name__ == "__main__":
    run()
