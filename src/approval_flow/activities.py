"""Temporal activities: thin wrappers over steps.py.

The workflow never calls steps.py directly — it calls these activities,
and Temporal's retry + history machinery applies to them. The wrappers
add nothing but the @activity.defn decorator and env lookup, so the
behaviour you test in tests/ (which calls steps.py) is the behaviour
that runs in production.

Activity names are explicit strings: workflows.py invokes activities by
name, so the workflow module never imports this one (Temporal's workflow
sandbox restricts imports — keeping the workflow file dependency-free
is what lets it replay safely).
"""

from __future__ import annotations

import os
from pathlib import Path

from temporalio import activity

from . import steps

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "state.db"


def _db() -> str:
    return os.environ.get("APPROVAL_FLOW_DB", str(DEFAULT_DB))


def _slack_url() -> str:
    return os.environ.get("MOCK_SLACK_URL", "http://127.0.0.1:18765")


@activity.defn(name="extract_invoice")
def extract_invoice(invoice_id: str) -> dict:
    return steps.extract_invoice(_db(), invoice_id)


@activity.defn(name="validate_invoice")
def validate_invoice(invoice_id: str) -> dict:
    return steps.validate_invoice(_db(), invoice_id)


@activity.defn(name="request_approval")
def request_approval(invoice_id: str) -> dict:
    return steps.request_approval(_db(), invoice_id, _slack_url())


@activity.defn(name="record_payment")
def record_payment(invoice_id: str) -> dict:
    return steps.record_payment(_db(), invoice_id)


@activity.defn(name="record_rejection")
def record_rejection(payload: dict) -> dict:
    return steps.record_rejection(_db(), payload["invoice_id"], payload["reason"])


ALL_ACTIVITIES = [
    extract_invoice,
    validate_invoice,
    request_approval,
    record_payment,
    record_rejection,
]
