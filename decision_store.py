"""SQLite-backed, idempotent proposal review decisions."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("data/reconciliation.db")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def fingerprint(proposal: dict) -> str:
    normalized = {str(k): "" if v is None else str(v).strip() for k, v in proposal.items()}
    body = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

def proposal_key(proposal: dict) -> str:
    """Stable identity across evidence/rationale enrichment and safe reruns."""
    if proposal.get("website_name"):
        body = "|".join(str(proposal.get(k, "")).strip().lower() for k in
                        ("website_name", "website_address", "website_city", "website_state", "website_zip"))
    else:
        body = f"crm|{proposal.get('crm_account_id', '')}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

class DecisionStore:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS proposals (
                fingerprint TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
                proposal_type TEXT NOT NULL, website_name TEXT, crm_account_id TEXT,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                decided_at TEXT, applied_at TEXT, error_message TEXT)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(proposals)")}
            if "proposal_key" not in columns:
                connection.execute("ALTER TABLE proposals ADD COLUMN proposal_key TEXT")
            if "rationale" not in columns:
                connection.execute("ALTER TABLE proposals ADD COLUMN rationale TEXT")
            connection.execute("""CREATE TABLE IF NOT EXISTS proposal_archive (
                archived_at TEXT NOT NULL, archive_reason TEXT NOT NULL,
                fingerprint TEXT NOT NULL, row_json TEXT NOT NULL)""")
            for row in connection.execute("SELECT fingerprint,payload_json FROM proposals WHERE proposal_key IS NULL"):
                key = proposal_key(json.loads(row["payload_json"]))
                existing = connection.execute("SELECT fingerprint FROM proposals WHERE proposal_key=?", (key,)).fetchone()
                if existing:
                    full = connection.execute("SELECT * FROM proposals WHERE fingerprint=?", (row["fingerprint"],)).fetchone()
                    connection.execute("INSERT INTO proposal_archive VALUES (?,?,?,?)",
                                       (_now(), "duplicate legacy row created during live schema migration",
                                        row["fingerprint"], json.dumps(dict(full), ensure_ascii=False)))
                    connection.execute("DELETE FROM proposals WHERE fingerprint=?", (row["fingerprint"],))
                else:
                    connection.execute("UPDATE proposals SET proposal_key=? WHERE fingerprint=?",
                                       (key, row["fingerprint"]))
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS proposals_proposal_key ON proposals(proposal_key)")

    def sync(self, proposals: list[dict]) -> list[str]:
        fingerprints, now = [], _now()
        with self.connect() as connection:
            for proposal in proposals:
                key = proposal_key(proposal)
                existing = connection.execute("SELECT fingerprint FROM proposals WHERE proposal_key=?", (key,)).fetchone()
                fp = existing["fingerprint"] if existing else fingerprint(proposal)
                fingerprints.append(fp)
                payload = json.dumps(proposal, sort_keys=True, ensure_ascii=False)
                connection.execute("""INSERT INTO proposals
                    (fingerprint,status,proposal_type,website_name,crm_account_id,payload_json,created_at,updated_at,proposal_key)
                    VALUES (?,'pending',?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET
                    payload_json=excluded.payload_json, proposal_type=excluded.proposal_type,
                    website_name=excluded.website_name, crm_account_id=excluded.crm_account_id,
                    proposal_key=excluded.proposal_key, updated_at=excluded.updated_at""",
                    (fp, proposal.get("proposal_type", ""), proposal.get("website_name", ""),
                     proposal.get("crm_account_id", ""), payload, now, now, key))
        return fingerprints

    def set_status(self, fp: str, status: str, error_message: str | None = None,
                   rationale: str | None = None) -> None:
        allowed = {"pending", "approved_pending_writeback", "approved_applied", "rejected", "writeback_failed"}
        if status not in allowed:
            raise ValueError(f"Unsupported decision status: {status}")
        now = _now()
        decided_at = now if status in {"approved_pending_writeback", "approved_applied", "rejected"} else None
        applied_at = now if status == "approved_applied" else None
        with self.connect() as connection:
            cursor = connection.execute("""UPDATE proposals SET status=?,updated_at=?,
                decided_at=COALESCE(?,decided_at),applied_at=COALESCE(?,applied_at),error_message=?,
                rationale=COALESCE(?,rationale) WHERE fingerprint=?""",
                (status, now, decided_at, applied_at, error_message, rationale, fp))
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown proposal fingerprint: {fp}")

    def rows(self, statuses: set[str] | None = None) -> list[dict]:
        query, params = "SELECT * FROM proposals", ()
        if statuses:
            query += f" WHERE status IN ({','.join('?' for _ in statuses)})"
            params = tuple(sorted(statuses))
        query += " ORDER BY created_at,website_name"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return {row["status"]: row["count"] for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM proposals GROUP BY status")}
