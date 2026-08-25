# Bellhaven CRM Ownership Reconciliation

A review-first system for reconciling Bellhaven's public facility portfolio with CRM account ownership. It turns website and CRM evidence into explainable proposals, requires a human decision, and permits only approved, guarded, verified writebacks.

## Problem statement

The website is the source of truth for Bellhaven's current facility ownership, while the CRM contains valuable account history, revenue, accounts receivable, and legacy ownership relationships. The challenge is to align the systems without destroying history, selecting an unsafe duplicate survivor, or treating a missing website listing as proof that a facility closed.

The assessment began with **35 website facilities** and **121 CRM accounts**.

## Architecture

```text
Bellhaven website ──> scraper.py ──> website_facilities.csv
                                         │
CRM API ────────────> crm.py ────────────┤
                                         v
                         matcher.py + pipeline.py
                                         │
                          explainable proposals.csv
                                         │
                         app.py + decision_store.py
                              human approve/reject
                                         │
                              approved queue only
                                         v
                            execute_approved.py
                           POST/PATCH + GET verify
```

- `scraper.py` validates and extracts the 35 public community records.
- `crm.py` provides paginated reads and guarded CRM mutations.
- `matcher.py` normalizes names, addresses, cities, states, and ZIP codes and produces explainable scores.
- `pipeline.py` classifies matches and snapshots the evidence needed for review.
- `decision_store.py` preserves proposals, decisions, rationales, and execution state in local SQLite.
- `app.py` is the human review UI; approval queues work but never writes automatically.
- `execute_approved.py` plans or executes only the approved queue and verifies every mutation with a GET.

## Matching strategy

Physical address and geography deliberately outrank name similarity. Facility names change after acquisitions, abbreviations vary, and legacy brands remain in CRM; an address, city, state, and ZIP combination is a stronger identity signal. Hard geographic conflicts prevent a familiar name from winning.

The deterministic matcher uses normalized street suffixes and directional terms, exact city/state/ZIP signals, address similarity, name similarity, score thresholds, and the margin over the second candidate. Each proposal includes the score, confidence, evidence, reason, proposed change, and relevant CRM snapshot.

### Proposal classifications

| Classification | Meaning |
|---|---|
| `NO_ACTION` | Name and Bellhaven parent already agree. |
| `RENAME` | Strong location match; website name differs. |
| `REPARENT` | Strong location match; CRM parent differs. |
| `RENAME_AND_REPARENT` | Both safe changes are required. |
| `CREATE_NEW` | No sufficiently strong CRM candidate exists. |
| `DUPLICATE` | Same-address accounts exist and one unique canonical survivor is safe. |
| `CHOW_CREATE_NEW` | Ownership changed and the historical account must be preserved. |
| `NEEDS_REVIEW` | Evidence is ambiguous or insufficient for a safe write. |

## CHOW standard operating procedure

When an existing account is under the wrong owner and has both lifetime revenue and outstanding AR, the system treats it as a change of ownership (CHOW):

1. Create a new Bellhaven child account using the current website identity and location.
2. Keep the historical account under its old parent.
3. Patch only `chow_current_account` on the old record with the newly returned account ID.
4. GET both records and verify the expected values.

The old account is never reparented. If creation succeeds but linking fails, execution stops and records `writeback_failed`; it does not automatically retry and risk creating a second account.

## Duplicate handling

Same-address records are writable only when exactly one record is the canonical Bellhaven name/parent survivor. Review evidence includes revenue, AR, status, update history, and existing duplicate/CHOW links. Approved losers are set to `Inactive`, linked through `duplicate_of_account`, and annotated; they are not deleted. When there is no unique safe survivor, the result remains `NEEDS_REVIEW`.

## Human review, idempotency, and writeback safety

The Streamlit UI shows website evidence beside CRM and financial/history evidence. Every approval or rejection requires a rationale. Approval changes local state to `approved_pending_writeback`; it does not contact the CRM.

SQLite keys proposals by stable facility/account identity, so pipeline reruns update evidence without losing decisions. Applied, rejected, and failed records are excluded from the approved queue, and existing CHOW/duplicate links prevent completed work from being proposed again.

Writeback has layered safeguards:

- `WRITEBACK_ENABLED=false` is the default.
- Dry-run is the executor default.
- Real writes require both `--execute` and `WRITEBACK_ENABLED=true`.
- Only `approved_pending_writeback` records are eligible.
- `NO_ACTION` and `NEEDS_REVIEW` generate no CRM operations.
- Payload fields are allow-listed and proposal shapes are validated.
- Account IDs and the unique Bellhaven parent are resolved before writing.
- Every POST/PATCH is followed by GET verification.
- Execution stops on the first failure and records it locally.

## Final CRM results

| Result | Count |
|---|---:|
| Initial CRM accounts | 121 |
| Accounts created | 5 |
| Rename/reparent PATCHes | 10 |
| Duplicate losers made inactive | 4 |
| CHOWs completed | 2 |
| Final CRM accounts | 126 |
| Converged `NO_ACTION` rows | 32 |
| Intentionally unresolved/rejected rows | 6 |
| Writeback failures | 0 |

All mutations passed immediate GET verification. After execution, a rerun converged to **32 `NO_ACTION`** and **6 `NEEDS_REVIEW`**, with no pending or approved writeback queue.

### Intentionally unresolved cases

| Case | Why no write was made |
|---|---|
| Union Square | Candidate street/address conflicts with the website evidence. |
| Kettering | Same-address records exist, but there is no unique safe duplicate survivor. |
| Owosso | Same-address records exist, but there is no unique safe duplicate survivor. |
| Alliance | Website absence alone does not establish closure or ownership disposition. |
| Coldwater | Website absence alone does not establish closure or ownership disposition. |
| Sandusky | Website absence alone does not establish closure or ownership disposition. |

This is intentional risk control: ambiguity produces review, not a destructive guess.

## Setup and commands

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add the assessment CRM token to the local `.env`. Never commit that file. Leave writeback disabled for scraping, matching, review, tests, and dry-runs.

```bash
# Scrape and validate the public portfolio
python scraper.py

# Generate proposals from website and current CRM state
python pipeline.py

# Open the human review UI
streamlit run app.py

# Preview the approved queue without writing
python execute_approved.py --dry-run
```

Run tests and a syntax smoke check:

```bash
python -m unittest discover -s tests -v
python -m py_compile scraper.py crm.py matcher.py pipeline.py decision_store.py app.py execute_approved.py
```

For an explicitly authorized writeback, set `WRITEBACK_ENABLED=true` only for the execution window and select a batch:

```bash
python execute_approved.py --batch simple --execute
python execute_approved.py --batch duplicate-create --execute
python execute_approved.py --batch chow --execute
```

Restore `WRITEBACK_ENABLED=false` immediately afterward, rerun the pipeline and tests, and confirm convergence.

## Daily schedule

`.github/workflows/daily.yml` runs daily at 12:17 UTC and on manual dispatch. It installs dependencies, runs tests, scrapes the current Bellhaven website, and regenerates proposals in read-only mode with `WRITEBACK_ENABLED=false`. `CRM_TOKEN` must be configured as a GitHub Actions secret. The workflow has read-only repository permissions, contains no execution step, and does not upload CRM evidence.

GitHub-hosted Actions runners are ephemeral, so the checked-in workflow primarily demonstrates read-only daily scheduling; its local SQLite reviewer decisions do not persist across workflow runs. A production deployment should keep the decision store on durable storage, for example by running cron on a persistent host or by using an external database. On a persistent host, the corresponding daily cron entry could be:

```cron
17 12 * * * cd /opt/bellhaven-reconciliation && /opt/bellhaven-reconciliation/.venv/bin/python scraper.py && /opt/bellhaven-reconciliation/.venv/bin/python pipeline.py
```

## Demo walkthrough

1. Frame the ownership/history risk and show the architecture.
2. Run or explain the 35-facility scraper validation.
3. Show address/geography-first matching and explainable evidence.
4. Walk through one straightforward rename/reparent case.
5. Use Marietta to demonstrate the two-step CHOW and preserved history.
6. Show a duplicate survivor decision and inactive loser linkage.
7. Show Union Square as an intentional refusal to guess.
8. Approve/reject in Streamlit and show rationale persistence.
9. Run the executor dry-run and inspect its exact planned operations.
10. Rerun the pipeline and show the final 32/6 converged state.

## Repository data policy

`data/website_facilities.csv` is retained because it is reproducible from the public website and makes the case study inspectable. Generated `data/proposals.csv`, reconciliation databases, backup databases, local environments, credentials, caches, and editor files are intentionally ignored because they contain CRM evidence or local state.

## Time spent

Actual focused time spent: **approximately 3 hours**
