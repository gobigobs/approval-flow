"""The Temporal workflow: invoice in, paid-or-rejected out.

Deliberately dependency-free (only temporalio + stdlib): Temporal replays
workflow code from its event history inside a sandbox, so workflow code
must be deterministic — no datetime.now(), no random, no I/O, no SQLite.
All I/O lives in activities; all decisions the workflow makes come from
activity results and signals.

Flow:
    extract -> validate -> [invalid: reject, stop]
           -> request approval card -> wait for human signal
           -> approved: record payment (idempotent) -> PAID
           -> rejected / timed out: record rejection -> REJECTED

If the worker dies while the workflow waits for a signal, Temporal keeps
the workflow's history on the server. A restarted worker replays the
history and resumes exactly at the wait — extract and validate are NOT
re-run. The demo kills the worker mid-wait to prove this.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow

from .models import APPROVAL_TIMEOUT_SECONDS


@workflow.defn
class InvoiceApprovalWorkflow:
    def __init__(self) -> None:
        self._decision: str | None = None
        self._reason: str = ""
        self._step: str = "started"

    @workflow.run
    async def run(self, invoice_id: str) -> dict:
        # Step 1 — extract.
        self._step = "extract"
        extracted = await workflow.execute_activity(
            "extract_invoice",
            invoice_id,
            start_to_close_timeout=timedelta(seconds=60),
        )

        # Step 2 — validate. Invalid invoices never reach a human.
        self._step = "validate"
        validation = await workflow.execute_activity(
            "validate_invoice",
            invoice_id,
            start_to_close_timeout=timedelta(seconds=60),
        )
        if not validation["valid"]:
            reason = "validation failed: " + "; ".join(validation["errors"])
            await workflow.execute_activity(
                "record_rejection",
                {"invoice_id": invoice_id, "reason": reason},
                start_to_close_timeout=timedelta(seconds=60),
            )
            self._step = "rejected"
            return {"status": "REJECTED", "reason": reason, "invoice_id": invoice_id}

        # Step 3 — ask a human, then wait for their signal.
        self._step = "request_approval"
        await workflow.execute_activity(
            "request_approval",
            invoice_id,
            start_to_close_timeout=timedelta(seconds=60),
        )
        self._step = "awaiting_approval"
        try:
            await asyncio.wait_for(
                workflow.wait_condition(lambda: self._decision is not None),
                timeout=APPROVAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._decision = "rejected"
            self._reason = (
                f"approval timed out after {APPROVAL_TIMEOUT_SECONDS // 60} minutes"
            )

        # Step 4 — act on the decision.
        if self._decision == "approved":
            self._step = "payment"
            payment = await workflow.execute_activity(
                "record_payment",
                invoice_id,
                start_to_close_timeout=timedelta(seconds=60),
            )
            self._step = "paid"
            return {
                "status": "PAID",
                "invoice_id": invoice_id,
                "amount_display": payment["amount_display"],
                "idempotency_key": payment["idempotency_key"],
                "payment_recorded": payment["recorded"],
            }

        self._step = "rejection"
        await workflow.execute_activity(
            "record_rejection",
            {"invoice_id": invoice_id, "reason": self._reason or "rejected"},
            start_to_close_timeout=timedelta(seconds=60),
        )
        self._step = "rejected"
        return {
            "status": "REJECTED",
            "invoice_id": invoice_id,
            "reason": self._reason or "rejected",
            "extracted": extracted,
        }

    @workflow.signal
    def approve(self, reason: str = "") -> None:
        self._decision = "approved"
        self._reason = reason or "approved"

    @workflow.signal
    def reject(self, reason: str = "") -> None:
        self._decision = "rejected"
        self._reason = reason or "rejected"

    @workflow.query
    def status(self) -> dict:
        return {
            "step": self._step,
            "decision": self._decision,
            "reason": self._reason,
        }
