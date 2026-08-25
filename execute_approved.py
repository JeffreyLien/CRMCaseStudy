"""Plan or execute CRM writes for explicitly approved reconciliation proposals.

Dry-run is the default.  Real execution additionally requires both --execute and
WRITEBACK_ENABLED=true; crm.py enforces the environment guard before each write.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crm import create_account, get_account, get_accounts, handle_chow, update_account
from decision_store import DecisionStore


DB = Path("data/reconciliation.db")
PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
APPROVED = {"approved_pending_writeback"}
NON_WRITING_TYPES = {"NO_ACTION", "NEEDS_REVIEW"}
BATCH_TYPES = {"simple": {"RENAME", "REPARENT", "RENAME_AND_REPARENT", "NO_ACTION"},
               "duplicate-create": {"DUPLICATE", "CREATE_NEW"}, "chow": {"CHOW_CREATE_NEW"}}


@dataclass(frozen=True)
class Operation:
    action: str
    account_id: str | None
    payload: dict[str, Any]


def _changes(proposal: dict) -> dict:
    raw = proposal.get("proposed_changes") or "{}"
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("proposed_changes must be a JSON object")
    return value


def _one_parent_id(accounts: list[dict]) -> str:
    matches = [a for a in accounts if a.get("name") == PARENT_NAME]
    if len(matches) != 1 or not matches[0].get("account_id"):
        raise ValueError(f"Expected exactly one CRM parent named {PARENT_NAME!r}; found {len(matches)}")
    return str(matches[0]["account_id"])


def _create_payload(proposal: dict, parent_id: str) -> dict:
    payload = {
        "name": proposal.get("website_name"),
        "parent_id": parent_id,
        "billing_street": proposal.get("website_address"),
        "billing_city": proposal.get("website_city"),
        "billing_state": proposal.get("website_state"),
        "billing_zip": proposal.get("website_zip"),
    }
    if not payload["name"]:
        raise ValueError("Create proposal has no website_name")
    return {key: value for key, value in payload.items() if value not in (None, "")}


def plan(proposal: dict, accounts: list[dict]) -> list[Operation]:
    """Convert one approved proposal to exact API operations without writing."""
    kind = proposal.get("proposal_type")
    changes = _changes(proposal)
    account_id = proposal.get("crm_account_id") or None

    if kind in NON_WRITING_TYPES:
        return []
    if kind == "CREATE_NEW":
        return [Operation("CREATE", None, _create_payload(proposal, _one_parent_id(accounts)))]
    if kind in {"RENAME", "REPARENT", "RENAME_AND_REPARENT"}:
        if not account_id:
            raise ValueError(f"{kind} requires crm_account_id")
        payload = {}
        if "name" in changes:
            payload["name"] = changes["name"]
        if "parent_name" in changes:
            if changes["parent_name"] != PARENT_NAME:
                raise ValueError(f"Unrecognized parent_name: {changes['parent_name']!r}")
            payload["parent_id"] = _one_parent_id(accounts)
        if not payload:
            raise ValueError(f"{kind} contains no supported changes")
        return [Operation("UPDATE", account_id, payload)]
    if kind == "DUPLICATE":
        retain = changes.get("retain_account")
        candidates = changes.get("review_duplicate_accounts")
        if not retain or not isinstance(candidates, list) or retain not in candidates:
            raise ValueError("DUPLICATE requires a retained account included in review_duplicate_accounts")
        losers = [str(value) for value in candidates if value != retain]
        if not losers:
            raise ValueError("DUPLICATE proposal has no loser account")
        return [Operation("DUPLICATE", loser, {
            "status": "Inactive",
            "duplicate_of_account": retain,
            "note": f"Duplicate of account {retain}; confirmed by Bellhaven reconciliation review.",
        }) for loser in losers]
    if kind == "STALE_UNDER_BELLHAVEN":
        if not account_id:
            raise ValueError("STALE_UNDER_BELLHAVEN requires crm_account_id")
        # The current pipeline emits review_account_status as evidence, not a
        # requested mutation.  Only explicit writable fields may be executed.
        writable = {k: changes[k] for k in ("status", "note") if k in changes}
        if not writable:
            raise ValueError("STALE proposal has no explicit status or note change; review_account_status is not writable")
        return [Operation("UPDATE", account_id, writable)]
    if kind == "CHOW_CREATE_NEW":
        if not account_id:
            raise ValueError("CHOW_CREATE_NEW requires the old crm_account_id")
        if changes.get("do_not_reparent_old_account") is not True:
            raise ValueError("CHOW proposal is missing do_not_reparent_old_account=true")
        create_fields = changes.get("create_new_account")
        old_fields = changes.get("set_on_old_account")
        if not isinstance(create_fields, dict) or old_fields != {"chow_current_account": "<new_account_id>"}:
            raise ValueError("CHOW proposal does not match the required two-step shape")
        create_payload = {
            "name": create_fields.get("name"),
            "parent_id": _one_parent_id(accounts),
            "billing_street": create_fields.get("address"),
            "billing_city": create_fields.get("city"),
            "billing_state": create_fields.get("state"),
            "billing_zip": create_fields.get("zip"),
        }
        create_payload = {k: v for k, v in create_payload.items() if v not in (None, "")}
        if not create_payload.get("name"):
            raise ValueError("CHOW create payload has no name")
        # The placeholder documents the second call. handle_chow substitutes
        # the returned ID and never sends parent_id to the old account.
        return [
            Operation("CHOW_CREATE", None, create_payload),
            Operation("CHOW_LINK_OLD", account_id, {"chow_current_account": "<new_account_id>"}),
        ]
    raise ValueError(f"Unsupported proposal_type: {kind!r}")


def _print_plan(fp: str, proposal: dict, operations: list[Operation]) -> None:
    label = proposal.get("website_name") or proposal.get("crm_name") or "Unnamed"
    if not operations:
        print(f"SKIP {proposal.get('proposal_type')} | {label} | {fp[:12]} | no CRM write")
    for operation in operations:
        target = f" /api/v1/accounts/{operation.account_id}" if operation.account_id else " /api/v1/accounts"
        print(f"WOULD {operation.action}{target} | {label} | {fp[:12]}")
        print(json.dumps(operation.payload, ensure_ascii=False, sort_keys=True))


def _verify(operation: Operation, response: dict, expected: dict) -> None:
    account_id = operation.account_id or response.get("account_id") or response.get("id") or response.get("data", {}).get("account_id")
    if not account_id:
        raise RuntimeError("Verification failed: response omitted account ID")
    actual = get_account(str(account_id))
    mismatches = {key: (value, actual.get(key)) for key, value in expected.items()
                  if value != "<new_account_id>" and actual.get(key) != value}
    if mismatches:
        raise RuntimeError(f"GET verification mismatch for {account_id}: {mismatches}")

def run(db_path: Path = DB, execute: bool = False, batch: str = "all") -> int:
    store = DecisionStore(db_path)
    rows = store.rows(APPROVED)
    if batch != "all": rows = [row for row in rows if row["proposal_type"] in BATCH_TYPES[batch]]
    print(f"Approved queue: {len(rows)}")
    if not rows:
        print("Nothing to do. pending/rejected/applied/failed proposals are not eligible.")
        return 0

    # This is a GET only. It resolves the canonical parent ID and validates IDs.
    accounts = get_accounts()
    account_ids = {str(a.get("account_id")) for a in accounts}
    planned: list[tuple[dict, dict, list[Operation]]] = []
    errors = 0
    for row in rows:
        proposal = json.loads(row["payload_json"])
        try:
            operations = plan(proposal, accounts)
            for operation in operations:
                if operation.account_id and operation.account_id not in account_ids:
                    raise ValueError(f"CRM account does not exist: {operation.account_id}")
            planned.append((row, proposal, operations))
            _print_plan(row["fingerprint"], proposal, operations)
        except Exception as exc:
            errors += 1
            print(f"INVALID {proposal.get('proposal_type')} | {row['fingerprint'][:12]} | {exc}")

    if errors:
        print(f"Validation failed for {errors} proposal(s); no writes attempted.")
        return 2
    if not execute:
        print(f"Dry-run complete: {sum(len(x[2]) for x in planned)} planned API operation(s); 0 writes sent.")
        return 0

    for row, proposal, operations in planned:
        fp = row["fingerprint"]
        try:
            kind = proposal["proposal_type"]
            if not operations:  # Approved NO_ACTION / NEEDS_REVIEW is consumed without a write.
                store.set_status(fp, "approved_applied")
            elif kind == "CHOW_CREATE_NEW":
                result = handle_chow(operations[1].account_id, operations[0].payload)
                _verify(operations[0], result["created"], operations[0].payload)
                _verify(operations[1], result["old_account_link"], {"chow_current_account": result["new_account_id"]})
                store.set_status(fp, "approved_applied")
            elif kind == "CREATE_NEW":
                response = create_account(operations[0].payload); _verify(operations[0], response, operations[0].payload)
                store.set_status(fp, "approved_applied")
            else:
                for operation in operations:
                    response = update_account(operation.account_id, operation.payload); _verify(operation, response, operation.payload)
                store.set_status(fp, "approved_applied")
        except Exception as exc:
            store.set_status(fp, "writeback_failed", str(exc))
            print(f"FAILED {kind} | {fp[:12]} | {exc}")
            return 1
    print(f"Execution complete: {len(planned)} proposal(s) applied.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run (also the default)")
    parser.add_argument("--execute", action="store_true", help="Attempt writes; still requires WRITEBACK_ENABLED=true")
    parser.add_argument("--batch", choices=["all", *BATCH_TYPES], default="all")
    args = parser.parse_args()
    if args.dry_run and args.execute:
        parser.error("choose either --dry-run or --execute")
    raise SystemExit(run(args.db, execute=args.execute, batch=args.batch))
