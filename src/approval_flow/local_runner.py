"""Faithful local durable runner — same steps, no Temporal server.

Used when no Temporal dev server is available (run.sh falls back to
this automatically) and by the resume unit test. It is NOT a fake
workflow: it calls the exact same steps.py functions the Temporal
activities wrap, persists the same SQLite ledger after every step, and
resumes from the ledger — a completed step is skipped, never re-run.

Run one invoice to a terminal state (blocks waiting for a human decision):
    python -m approval_flow.local_runner --invoice INV-2026-1042

Env: APPROVAL_FLOW_DB, MOCK_SLACK_URL (same as the worker).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import steps, store
from .models import APPROVAL_TIMEOUT_SECONDS

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "state.db"


def run_invoice(
    db_path: str | Path,
    invoice_id: str,
    slack_url: str,
    timeout: int = APPROVAL_TIMEOUT_SECONDS,
    poll_seconds: float = 1.0,
) -> dict:
    """Run one invoice to PAID or REJECTED, resuming from the ledger."""
    wf = f"invoice-{invoice_id}"
    conn = store.connect(db_path)
    inv = store.get_invoice(conn, invoice_id)
    conn.close()
    if inv is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    if inv["status"] in ("PAID", "REJECTED"):
        return {"status": inv["status"], "invoice_id": invoice_id,
                "reason": inv["reason"], "resumed": True}

    # Step 1+2 — skipped automatically if the ledger says they completed.
    steps.extract_invoice(db_path, invoice_id)
    validation = steps.validate_invoice(db_path, invoice_id)
    if not validation["valid"]:
        reason = "validation failed: " + "; ".join(validation["errors"])
        steps.record_rejection(db_path, invoice_id, reason)
        return {"status": "REJECTED", "invoice_id": invoice_id, "reason": reason}

    # Step 3 — card (skipped if already posted).
    steps.request_approval(db_path, invoice_id, slack_url)

    # Wait for the human decision, polling the ledger. If this process is
    # killed here, the next run resumes after this point: extract,
    # validate and request_approval are already marked completed.
    deadline = time.monotonic() + timeout
    decision = None
    while time.monotonic() < deadline:
        conn = store.connect(db_path)
        decision = store.get_decision(conn, wf)
        conn.close()
        if decision:
            break
        time.sleep(poll_seconds)
    if decision is None:
        reason = f"approval timed out after {timeout // 60} minutes"
        steps.record_rejection(db_path, invoice_id, reason)
        return {"status": "REJECTED", "invoice_id": invoice_id, "reason": reason}

    if decision["decision"] == "approved":
        payment = steps.record_payment(db_path, invoice_id)
        return {"status": "PAID", "invoice_id": invoice_id,
                "amount_display": payment["amount_display"],
                "idempotency_key": payment["idempotency_key"],
                "payment_recorded": payment["recorded"]}
    steps.record_rejection(db_path, invoice_id,
                           decision["reason"] or "rejected")
    return {"status": "REJECTED", "invoice_id": invoice_id,
            "reason": decision["reason"] or "rejected"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Local durable runner (one invoice)")
    ap.add_argument("--invoice", required=True)
    ap.add_argument("--db", default=os.environ.get(
        "APPROVAL_FLOW_DB", str(DEFAULT_DB)))
    ap.add_argument("--slack-url", default=os.environ.get(
        "MOCK_SLACK_URL", "http://127.0.0.1:18765"))
    ap.add_argument("--timeout", type=int, default=APPROVAL_TIMEOUT_SECONDS)
    args = ap.parse_args()
    result = run_invoice(args.db, args.invoice, args.slack_url, args.timeout)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
