import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"
TOKEN = os.environ["CRM_TOKEN"]

HEADERS = {
    "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"
}

class WritebackDisabledError(RuntimeError):
    pass

def writeback_enabled() -> bool:
    return os.getenv("WRITEBACK_ENABLED", "false").strip().lower() == "true"

def _require_writeback() -> None:
    if not writeback_enabled():
        raise WritebackDisabledError("CRM writeback blocked: WRITEBACK_ENABLED is not true. No CRM request was sent.")

def update_account(account_id: str, changes: dict) -> dict:
    _require_writeback()
    response = requests.patch(f"{BASE_URL}/accounts/{account_id}", headers=HEADERS, json=changes, timeout=30)
    response.raise_for_status()
    return response.json()

def create_account(fields: dict) -> dict:
    _require_writeback()
    response = requests.post(f"{BASE_URL}/accounts", headers=HEADERS, json=fields, timeout=30)
    response.raise_for_status()
    return response.json()

def get_account(account_id: str) -> dict:
    response = requests.get(f"{BASE_URL}/accounts/{account_id}", headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()

def handle_duplicate(retain_account: str, duplicate_accounts: list[str]) -> list[dict]:
    _require_writeback()
    return [update_account(account_id, {"status": "Inactive", "duplicate_of_account": retain_account})
            for account_id in duplicate_accounts if account_id != retain_account]

def handle_chow(old_account_id: str, create_fields: dict) -> dict:
    """Create current account, then link old account; never reparent the old account."""
    _require_writeback()
    created = create_account(create_fields)
    new_id = created.get("account_id") or created.get("id") or created.get("data", {}).get("account_id")
    if not new_id:
        raise RuntimeError("Create response omitted account ID; old account was not changed.")
    linked = update_account(old_account_id, {"chow_current_account": new_id})
    return {"created": created, "old_account_link": linked, "new_account_id": new_id}


def get_accounts():
    all_accounts = []
    page = 1

    while True:
        response = requests.get(
            f"{BASE_URL}/accounts",
            headers=HEADERS,
            params={"page": page, "page_size": 50},
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()
        accounts = payload["data"]
        all_accounts.extend(accounts)

        print(f"Fetched page {page}: {len(accounts)} accounts")

        if len(all_accounts) >= payload["total"]:
            break

        page += 1

    return all_accounts


if __name__ == "__main__":
    accounts = get_accounts()
    print(f"\nTotal accounts fetched: {len(accounts)}")
