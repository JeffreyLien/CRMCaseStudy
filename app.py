"""Local review UI. Approval records a decision but never auto-executes CRM writes."""
from __future__ import annotations
import csv
import json
from pathlib import Path
import streamlit as st
from crm import writeback_enabled
from decision_store import DecisionStore

PROPOSALS = Path("data/proposals.csv")
DB = Path("data/reconciliation.db")

def load_proposals() -> list[dict]:
    with PROPOSALS.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))

st.set_page_config(page_title="Bellhaven Reconciliation Review", layout="wide")
st.title("Bellhaven CRM reconciliation")
if writeback_enabled():
    st.error("WRITEBACK IS ENABLED. This review screen still records approval without executing it.")
else:
    st.warning("Writeback disabled — approvals are queued only; no CRM changes can be sent.")

store = DecisionStore(DB)
store.sync(load_proposals())
records, counts = store.rows(), store.counts()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Proposal rows", len(records)); c2.metric("Pending", counts.get("pending", 0))
c3.metric("Approved / queued", counts.get("approved_pending_writeback", 0)); c4.metric("Rejected", counts.get("rejected", 0))
status_filter = st.multiselect("Decision status", sorted(counts), default=["pending"] if counts.get("pending") else sorted(counts))
types = sorted({r["proposal_type"] for r in records})
type_filter = st.multiselect("Proposal type", types, default=types)
visible = [r for r in records if r["status"] in status_filter and r["proposal_type"] in type_filter]
st.caption(f"Showing {len(visible)} of {len(records)} records. Decisions are preserved across reruns.")

for record in visible:
    proposal = json.loads(record["payload_json"])
    title = proposal.get("website_name") or proposal.get("crm_name") or "Unnamed account"
    with st.expander(f"{proposal['proposal_type']} · {title} · {record['status']}"):
        if proposal["proposal_type"] == "CHOW_CREATE_NEW":
            st.error("CHOW SAFETY: Do not reparent the old account. Step 1: create a new Bellhaven account. Step 2: set chow_current_account on the old account to the new ID.")
        left, right = st.columns(2)
        with left:
            st.subheader("Website evidence")
            st.write({"name": proposal.get("website_name"), "address": proposal.get("website_address"),
                      "city": proposal.get("website_city"), "state": proposal.get("website_state"), "zip": proposal.get("website_zip")})
        with right:
            st.subheader("Current CRM")
            st.write({"account_id": proposal.get("crm_account_id"), "name": proposal.get("crm_name"), "parent": proposal.get("crm_parent")})
            try:
                evidence_rows = json.loads(proposal.get("crm_evidence") or "[]")
            except json.JSONDecodeError:
                evidence_rows = []
            if evidence_rows:
                st.dataframe(evidence_rows, use_container_width=True, hide_index=True)
        st.write(f"**Match score:** {proposal.get('match_score') or 'N/A'} | **Confidence:** {proposal.get('confidence') or 'N/A'}")
        st.write(f"**Reason:** {proposal.get('reason')}"); st.write(f"**Evidence:** {proposal.get('evidence')}")
        st.subheader("Proposed changes")
        try: changes = json.loads(proposal.get("proposed_changes") or "{}")
        except json.JSONDecodeError: changes = {"raw": proposal.get("proposed_changes")}
        st.json(changes)
        if record["status"] == "pending":
            rationale = st.text_area("Review rationale (required)", key=f"rationale-{record['fingerprint']}")
            approve, reject = st.columns(2)
            if approve.button("Approve (queue only)", key=f"approve-{record['fingerprint']}", use_container_width=True):
                if not rationale.strip(): st.error("Enter a rationale before approving.")
                else:
                    store.set_status(record["fingerprint"], "approved_pending_writeback", rationale=rationale.strip())
                    st.success("Approved and queued. No CRM write was attempted."); st.rerun()
            if reject.button("Reject", key=f"reject-{record['fingerprint']}", use_container_width=True):
                if not rationale.strip(): st.error("Enter a rationale before rejecting.")
                else:
                    store.set_status(record["fingerprint"], "rejected", rationale=rationale.strip())
                    st.info("Rejected. CRM was not contacted."); st.rerun()
        else:
            st.info(f"Decision saved: {record['status']} — {record.get('rationale') or 'legacy decision; rationale pending'}")
