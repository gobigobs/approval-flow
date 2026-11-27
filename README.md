# approval-flow

**An invoice-approval workflow that survives a dead worker: extract → validate → ask a human in Slack → pay once, exactly once.**

![100% local](https://img.shields.io/badge/100%25%20local-no%20keys%2C%20no%20cloud-121317)
![Real Temporal](https://img.shields.io/badge/Temporal-real%20dev%20server%201.32.2-7FB685)
![Tests](https://img.shields.io/badge/tests-10%20passing-7FB685)
![License](https://img.shields.io/badge/license-MIT-FAF7F2)

A supplier invoice arrives as text. The workflow extracts its fields, validates amount / vendor / duplicates, posts an approval card to a Slack-style channel, waits for a human, and then pays or rejects. If the worker dies mid-run, the workflow resumes from the last completed step instead of starting over. If the payment step is retried, the payment is recorded exactly once.

No real Slack workspace, no real money, no API keys. The "payment" is a ledger row guarded by an idempotency key — the mechanism is real, the money is not.

## What it looks like

Approval cards waiting for a human (real screenshot of the running demo):

![Approval cards awaiting decision](docs/demo-approval-card.png)

After the decisions — two paid, one rejected with its reason (real screenshot):

![Final state: two paid, one rejected](docs/demo-final.png)

> Both screenshots are real captures of the running mock-Slack dashboard (`http://127.0.0.1:18765/`), taken by the demo itself with headless Chromium — not mockups.

## Quick start — one command

```bash
./run.sh            # full demo on REAL Temporal (dev server, auto-downloaded)
./run.sh --local    # same demo on the local durable runner (no Temporal)
```

Requirements: Python 3.12 and internet once (pip installs `temporalio`; the first Temporal run downloads the official `temporal` CLI, ~38 MB, into `.tools/`). Everything runs in a repo-local `.venv`; system Python is untouched. Node + Playwright are needed only for the screenshots — if they are missing the demo logs a warning and continues.

The demo seeds 3 invoices, runs all 3 workflows, kills the worker mid-run, restarts it, approves one, rejects one, replays a payment, and prints the final state. It takes about 2 minutes in Temporal mode, ~15 seconds in local mode.

## Measured results — 25 Nov 2026, this machine

`./run.sh` (Temporal mode) and `./run.sh --local` both passed end to end. The Temporal run's log is `docs/demo-run.log`; its summary is `docs/demo-summary.json`.

| Invoice | Vendor | Amount | Decision | Final state |
|---|---|---|---|---|
| INV-2026-1042 | Varagi Trades Hyper | Rs 18,450.00 | Approved | **PAID** |
| INV-2026-1043 | Savitha Mart | Rs 7,290.00 | Rejected — "Duplicate of last month's order — verify with Savitha Mart before paying" | **REJECTED** |
| INV-2026-1044 | All Food Supplies | Rs 52,300.00 | Approved (after worker restart) | **PAID** |

**Restart-resume proof (Temporal mode):** the worker was SIGTERM'd while all 3 workflows waited for approval, then restarted. The INV-2026-1044 workflow was still `RUNNING` on the server afterwards, and the ledger shows its extract step ran exactly **once** (`extract runs = 1`) — it resumed at the wait, it did not re-extract or re-validate.

**Idempotency proof:** replaying the INV-2026-1042 payment with the same key `pay:INV-2026-1042:1845000` returned `recorded=False`, and the payments table holds exactly **1** row for that key. Total payments recorded: **2** (1042 + 1044). The pytest idempotency test (`tests/test_idempotency.py`) also passes live inside the demo.

**Tests:** `PYTHONPATH=src .venv/bin/python -m pytest tests/ -q` → **10 passed** (validation ×6, idempotency ×2, resume ×2).

## How it works

```
seeded invoice text (data/invoices/INV-*.txt)
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  InvoiceApprovalWorkflow  (src/approval_flow/workflows.py)     │
│  Temporal workflow — deterministic, no I/O, no clocks           │
│                                                                 │
│  extract ──▶ validate ──▶ request approval ──▶ wait for signal  │
│     │           │               │                    │          │
│     │      invalid?            │              approved?        │
│     │         ▼                ▼                    │          │
│     │      REJECTED      card posted         ┌─────┴─────┐     │
│     │      (reason)      to mock Slack       ▼           ▼     │
│     │                              record payment   record     │
│     │                              (idempotent)      rejection  │
│     └──────────────────────────────────────────────▶ PAID / REJECTED
└─────────────────────────────────────────────────────────────────┘
        │ activities (Temporal retries apply)                     │
        ▼                                                         ▼
  SQLite ledger (data/state.db)                     mock Slack server
  steps · payments · decisions · cards             (stdlib http.server)
  — every step writes here after it completes       http://127.0.0.1:18765/
```

**Two durability layers, on purpose:**

1. **Temporal's event history** — the workflow's state lives on the Temporal server. Kill the worker and the workflow keeps waiting; a restarted worker replays history and resumes at the exact wait. This is the real durability story.
2. **The SQLite ledger** (`data/state.db`) — an independent, human-readable record: which step completed, how many times each step ran (`steps.runs`), which payments exist (primary key = idempotency key), and what each human decided. You can inspect it with plain `sqlite3` after any run. The local runner uses layer 2 alone.

**Idempotency, concretely:** the payment key is deterministic — `pay:<invoice-id>:<amount-in-paise>` — never random, never a timestamp. `record_payment` does `INSERT OR IGNORE` on that key: the second call inserts zero rows and reports `recorded=False`. Extract / validate / request-approval are idempotent the same way: if the ledger says the step already completed, the activity returns the cached result instead of doing the work again (so a Temporal activity retry after a crash cannot post a second approval card either).

**Validation rules** (`steps.validate_invoice`): amount must be positive and ≤ Rs 1,00,000; vendor must be on the known-vendor list; same vendor + same amount + same date as another non-rejected invoice is a probable duplicate. Anything failing validation is rejected with the reason and never reaches a human.

**The human decision** arrives as a Temporal **signal** (`approve` / `reject`) sent by the mock Slack server when a button is clicked. In local mode the server records the decision in the ledger and the runner polls it. The workflow waits up to 10 minutes for a decision, then times out as a rejection.

## Project layout

```
approval-flow/
├── run.sh                    # one-command demo (Temporal, or --local)
├── demo.py                   # demo orchestrator: seed → run → kill → restart → decide → prove
├── requirements.txt          # temporalio==1.37.0 (only runtime dep)
├── requirements-dev.txt      # pytest
├── data/invoices/            # 3 seeded invoice texts (PDF-like, no OCR)
├── src/approval_flow/
│   ├── workflows.py          # the Temporal workflow (deterministic, I/O-free)
│   ├── activities.py         # Temporal activities — thin wrappers over steps.py
│   ├── steps.py              # the steps themselves, shared by both runners
│   ├── store.py              # SQLite ledger (steps / payments / decisions / cards)
│   ├── worker.py             # Temporal worker (task queue: approval-flow)
│   ├── local_runner.py       # faithful local runner: same steps, ledger resume
│   ├── mock_slack.py         # mock Slack: cards, dashboard, approve/reject buttons
│   ├── seed.py               # deterministic seed of the 3 invoices
│   └── models.py             # Invoice model, amounts in integer paise, limits
├── tests/                    # validation, idempotency, resume (10 tests)
└── docs/
    ├── demo-approval-card.png  # real screenshot: cards awaiting approval
    ├── demo-final.png          # real screenshot: 2 paid, 1 rejected
    ├── demo-run.log            # full log of the last demo run
    ├── demo-summary.json       # machine-readable results of the last demo run
    └── capture.mjs             # Playwright screenshot script used by the demo
```

## Try it yourself

```bash
# Leave the stack up and play with it by hand:
./run.sh --serve
#   mock Slack dashboard: http://127.0.0.1:18765/
#   Temporal UI:          http://127.0.0.1:8233/  (namespaces → default → workflows)
#   then, in another shell, start one workflow:
.venv/bin/python - <<'EOF'
import asyncio
from temporalio.client import Client
from approval_flow.workflows import InvoiceApprovalWorkflow
async def main():
    c = await Client.connect("localhost:7233")
    h = await c.start_workflow(InvoiceApprovalWorkflow.run, "INV-2026-1042",
                               id="invoice-INV-2026-1042", task_queue="approval-flow")
    print("result:", await h.result())
asyncio.run(main())
EOF
# Approve or reject it by clicking the card at http://127.0.0.1:18765/

# Or run a single invoice on the local runner (no Temporal at all):
./run.sh --serve-local   # in one shell
.venv/bin/python -m approval_flow.local_runner --invoice INV-2026-1042

# Inspect the ledger after any run:
sqlite3 data/state.db "SELECT invoice_id, status FROM invoices;"
sqlite3 data/state.db "SELECT workflow_id, step, runs FROM steps ORDER BY 1, 2;"
sqlite3 data/state.db "SELECT idempotency_key FROM payments;"
```

## Honest scope: what is real, and what is not

| Piece | Status |
|---|---|
| Temporal workflow, activities, signals, worker | **Real.** Runs on the genuine Temporal server (dev server 1.32.2, `temporalio` 1.37.0) via the official CLI, downloaded by `run.sh`. The kill-and-restart resume was demonstrated against that server, not simulated. |
| Durable state | **Real, two layers.** Temporal event history + the SQLite ledger written after every step. |
| Idempotency | **Real.** Deterministic keys + `INSERT OR IGNORE`; the replay proof runs against the live ledger in every demo. |
| Approval channel | **Mock.** A local stdlib HTTP server that looks and behaves like a Slack channel (cards, buttons, JSON webhook). No real Slack workspace, no tokens, nothing leaves the machine. Swapping it for real Slack means replacing one POST target with Slack's Block Kit + a signing-secret check — the workflow code does not change. |
| Invoice extraction | **Seeded text, regex-parsed.** No OCR, no PDF parsing, no LLM. A real deployment would plug in a document parser at the extract step; the step's interface (invoice id in, fields out) stays the same. |
| Payment | **Ledger row only.** No bank, UPI, or payment gateway is called. The idempotency mechanism is the part being demonstrated; wiring a real payout means calling the gateway inside `record_payment` with the same key. |
| Local runner (`--local`) | **Faithful fallback, not a second implementation.** It calls the same `steps.py` functions and the same ledger as the Temporal activities; only the orchestration differs (ledger polling instead of Temporal history). It exists so the project still runs and still proves resume + idempotency where no Temporal server can start. |
| Scale / production | **Not claimed.** Single dev server on SQLite, one worker, no auth, no TLS, no multi-tenant isolation. For production: Temporal Cloud or a self-hosted cluster, a real database, Slack OAuth + signing secrets, and a real payment provider — listed here as work, not done. |

## What it does not do

- No secrets anywhere: no API keys, tokens, or passwords in the repo (there is nothing to steal — the mock Slack has no auth because it only listens on localhost).
- No invented results: every number in "Measured results" comes from the demo runs on 25 Nov 2026 (`docs/demo-run.log`, `docs/demo-summary.json`). Re-run `./run.sh` and the numbers are reproduced.
- No GitHub push is part of this repo's workflow; publishing is a separate, explicit decision.
