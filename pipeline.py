"""Build a review-only Bellhaven/CRM reconciliation proposal table."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from crm import get_accounts
from matcher import normalize_name, rank_candidates
from decision_store import DecisionStore

DEFAULT_PARENT = "Bellhaven Senior Living (Parent Account)"
FIELDS = [
    "website_name", "website_address", "website_city", "website_state", "website_zip",
    "crm_account_id", "crm_name", "crm_parent", "match_score", "confidence",
    "proposal_type", "proposed_changes", "evidence", "reason",
    "crm_evidence",
]


def confidence(score: float, margin: float, exact_address: bool) -> str:
    if exact_address and score >= 90 and margin >= 8:
        return "HIGH"
    if score >= 76 and margin >= 10:
        return "HIGH"
    if score >= 65 and margin >= 5:
        return "MEDIUM"
    return "LOW"


def is_bellhaven_parent(account: dict) -> bool:
    return account.get("parent_name") == DEFAULT_PARENT


def evidence_text(signals: dict, margin: float, candidate_count: int = 1) -> str:
    parts = [
        f"address_exact={signals['address_exact']}", f"address_similarity={signals['address_similarity']:.3f}",
        f"city_exact={signals['city_exact']}", f"state_exact={signals['state_exact']}",
        f"zip_exact={signals['zip_exact']}", f"name_similarity={signals['name_similarity']:.3f}",
        f"score_margin={margin:.2f}",
    ]
    if candidate_count > 1:
        parts.append(f"same_address_candidates={candidate_count}")
    return "; ".join(parts)


def base_row(facility: dict, account: dict | None = None) -> dict:
    account = account or {}
    return {
        "website_name": facility.get("name", ""),
        "website_address": facility.get("address", ""),
        "website_city": facility.get("city", ""), "website_state": facility.get("state", ""),
        "website_zip": facility.get("zip", ""), "crm_account_id": account.get("account_id", ""),
        "crm_name": account.get("name", ""), "crm_parent": account.get("parent_name", ""),
    }

def account_evidence(account: dict, signals: dict | None = None) -> dict:
    fields = ("account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city",
              "billing_state", "billing_zip", "status", "lifetime_revenue", "outstanding_ar",
              "updated_at", "chow_current_account", "duplicate_of_account", "note", "created_by_candidate")
    result = {key: account.get(key, "") for key in fields}
    if signals:
        result["match_signals"] = signals
    return result


def classify(facility: dict, ranked: list[tuple[dict, dict]]) -> tuple[dict, set[str]]:
    best, signals = ranked[0]
    second_score = ranked[1][1]["score"] if len(ranked) > 1 else 0
    margin = signals["score"] - second_score
    row = base_row(facility, best)

    exact_address_matches = [(a, s) for a, s in ranked if s["address_exact"] and s["city_exact"] and s["state_exact"]]
    matched_ids: set[str] = set()
    if len(exact_address_matches) > 1:
        # Prefer the canonical Bellhaven identity, then financial/history-bearing records.
        survivors = sorted(exact_address_matches, key=lambda x: (
            not is_bellhaven_parent(x[0]), -x[1]["name_similarity"],
            -(float(x[0].get("lifetime_revenue") or 0) + float(x[0].get("outstanding_ar") or 0)),
            x[0]["account_id"]))
        best, signals = survivors[0]
        matched_ids = {a["account_id"] for a, _ in exact_address_matches}
        row.update(base_row(facility, best))
        canonical = [(a, s) for a, s in exact_address_matches
                     if is_bellhaven_parent(a) and normalize_name(a.get("name")) == normalize_name(facility.get("name"))]
        if len(canonical) != 1:
            row.update(match_score=signals["score"], confidence="LOW", proposal_type="NEEDS_REVIEW")
            ids = [a["account_id"] for a, _ in exact_address_matches]
            row["proposed_changes"] = json.dumps({"review_duplicate_accounts": ids,
                                                   "reconciliation_note": "No unique canonical Bellhaven survivor; do not inactivate any account."})
            row["evidence"] = evidence_text(signals, margin, len(ids))
            row["crm_evidence"] = json.dumps([account_evidence(a, s) for a, s in exact_address_matches], ensure_ascii=False)
            row["reason"] = "Same-address accounts exist, but CRM evidence does not identify one safe survivor."
            return row, matched_ids
        best, signals = canonical[0]
        row.update(base_row(facility, best))
        row.update(match_score=signals["score"], confidence="HIGH", proposal_type="DUPLICATE")
        ids = [a["account_id"] for a, _ in exact_address_matches]
        row["proposed_changes"] = json.dumps({"retain_account": best["account_id"], "review_duplicate_accounts": ids})
        row["evidence"] = evidence_text(signals, margin, len(ids))
        row["crm_evidence"] = json.dumps([account_evidence(a, s) for a, s in exact_address_matches], ensure_ascii=False)
        row["reason"] = "Multiple CRM accounts share the normalized physical address and geography."
        return row, matched_ids

    score = signals["score"]
    conf = confidence(score, margin, signals["address_exact"])
    plausible = signals["address_exact"] or (score >= 68 and signals["city_exact"] and signals["state_exact"])
    # A close race between weak candidates is not ambiguity; it is simply no match.
    # Reserve review for a genuinely plausible same-geography/name lead (Union Square).
    ambiguous = not signals["address_exact"] and score >= 52
    if not plausible:
        if not ambiguous:
            row.update(crm_account_id="", crm_name="", crm_parent="")
        row.update(match_score=score, confidence="LOW",
                   proposal_type="NEEDS_REVIEW" if ambiguous else "CREATE_NEW")
        row["proposed_changes"] = json.dumps({"create_under": DEFAULT_PARENT} if not ambiguous else {"review_candidate": best["account_id"]})
        row["evidence"] = evidence_text(signals, margin)
        row["reason"] = "Possible but geographically/address-ambiguous candidate." if ambiguous else "No sufficiently strong CRM candidate."
        return row, set()

    matched_ids.add(best["account_id"])
    rename = facility["name"].strip() != best.get("name", "").strip()
    reparent = not is_bellhaven_parent(best)
    changes = {}
    if rename:
        changes["name"] = facility["name"]
    if reparent:
        changes["parent_name"] = DEFAULT_PARENT

    revenue = float(best.get("lifetime_revenue") or 0)
    ar = float(best.get("outstanding_ar") or 0)
    if reparent and revenue > 0 and ar > 0:
        proposal = "CHOW_CREATE_NEW"
        changes = {
            "create_new_account": {"name": facility["name"], "parent_name": DEFAULT_PARENT,
                                   "address": facility["address"], "city": facility["city"],
                                   "state": facility["state"], "zip": facility["zip"]},
            "set_on_old_account": {"chow_current_account": "<new_account_id>"},
            "do_not_reparent_old_account": True,
        }
        reason = f"Wrong/missing parent and CHOW guard applies (lifetime_revenue={revenue:g}, outstanding_ar={ar:g})."
    elif rename and reparent:
        proposal, reason = "RENAME_AND_REPARENT", "Strong location match; CRM name and parent both differ."
    elif reparent:
        proposal, reason = "REPARENT", "Strong location match; CRM parent is missing or not Bellhaven."
    elif rename:
        proposal, reason = "RENAME", "Strong location match; CRM name differs from the website."
    else:
        proposal, reason = "NO_ACTION", "CRM name and Bellhaven parent already agree."

    row.update(match_score=score, confidence=conf, proposal_type=proposal,
               proposed_changes=json.dumps(changes), evidence=evidence_text(signals, margin), reason=reason)
    row["crm_evidence"] = json.dumps([account_evidence(best, signals)], ensure_ascii=False)
    return row, matched_ids


def stale_rows(accounts: list[dict], matched_ids: set[str]) -> list[dict]:
    rows = []
    for account in accounts:
        if account.get("duplicate_of_account") or account.get("chow_current_account"):
            continue  # already reconciled loser/legacy CHOW account
        if not is_bellhaven_parent(account) or account.get("account_id") in matched_ids:
            continue
        # Parent container itself has no parent and is excluded naturally.
        row = base_row({}, account)
        note = "Website absence alone is insufficient to inactivate this active CRM account; verify closure, sale, or canonical disposition."
        row.update(match_score="", confidence="LOW", proposal_type="NEEDS_REVIEW",
                   proposed_changes=json.dumps({"reconciliation_note": note}),
                   evidence="CRM parent is Bellhaven; no website facility matched. No closure/ownership evidence is available.",
                   reason=note)
        row["crm_evidence"] = json.dumps([account_evidence(account)], ensure_ascii=False)
        rows.append(row)
    return rows


def run(website_path: Path, output_path: Path) -> list[dict]:
    with website_path.open(newline="", encoding="utf-8-sig") as handle:
        facilities = list(csv.DictReader(handle))
    accounts = get_accounts()
    matchable_accounts = [account for account in accounts
                          if not account.get("duplicate_of_account") and not account.get("chow_current_account")]
    proposals, matched_ids = [], set()
    for facility in facilities:
        row, ids = classify(facility, rank_candidates(facility, matchable_accounts))
        proposals.append(row)
        matched_ids.update(ids)
    proposals.extend(stale_rows(accounts, matched_ids))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader(); writer.writerows(proposals)
    DecisionStore().sync(proposals)
    print(f"Website facilities: {len(facilities)}; CRM accounts: {len(accounts)}; proposal rows: {len(proposals)}")
    print("Proposal counts:", dict(sorted(Counter(row["proposal_type"] for row in proposals).items())))
    return proposals


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--website", type=Path, default=Path("data/website_facilities.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/proposals.csv"))
    args = parser.parse_args()
    run(args.website, args.output)
