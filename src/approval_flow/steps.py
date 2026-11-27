"""The workflow's steps, written once and used by BOTH runners.

- The Temporal activities in activities.py are thin wrappers over these.
- The local runner in local_runner.py calls these directly.

That is what makes the local runner faithful instead of a second,
divergent implementation: same functions, same SQLite ledger, same
idempotency rules. Only the orchestration (Temporal history vs. ledger
polling) differs.

Every step is idempotent:
- extract / validate / request-approval return the cached result if the
  step already completed for this workflow.
- record_payment inserts with INSERT OR IGNORE on the idempotency key,
  so a retry records the payment exactly once.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

from . import store
from .models import KNOWN_VENDORS, MAX_AUTO_AMOUNT_PAISE, paise_to_rupees

_FIELD_RE = {
    "vendor": re.compile(r"^Vendor:\s*(.+)$", re.M),
    "invoice_date": re.compile(r"^Date:\s*(\d{4}-\d{2}-\d{2})$", re.M),
    "due_date": re.compile(r"^Due Date:\s*(\d{4}-\d{2}-\d{2})$", re.M),
    "currency": re.compile(r"^Currency:\s*([A-Z]{3})$", re.M),
    "description": re.compile(r"^Description:\s*(.+)$", re.M),
    "amount": re.compile(r"^Amount:\s*([\d,]+\.\d{2})$", re.M),
}


def parse_invoice_text(text: str) -> dict:
    """Parse a seeded invoice text file (PDF-like plain text, no OCR).

    Raises ValueError listing the fields it could not find, so a bad
    extract fails loudly instead of inventing numbers.
    """
    found: dict[str, str] = {}
    for key, rx in _FIELD_RE.items():
        m = rx.search(text)
        if m:
            found[key] = m.group(1).strip()
    missing = [k for k in ("vendor", "invoice_date", "amount") if k not in found]
    if missing:
        raise ValueError(f"could not extract fields: {', '.join(missing)}")
    amount_paise = int(round(float(found["amount"].replace(",", "")) * 100))
    return {
        "vendor": found["vendor"],
        "invoice_date": found["invoice_date"],
        "due_date": found.get("due_date"),
        "currency": found.get("currency", "INR"),
        "description": found.get("description"),
        "amount_paise": amount_paise,
        "amount_display": paise_to_rupees(amount_paise),
    }


def _workflow_id(invoice_id: str) -> str:
    return f"invoice-{invoice_id}"


def extract_invoice(db_path: str | Path, invoice_id: str) -> dict:
    """Step 1 — extract fields from the seeded invoice text."""
    conn = store.connect(db_path)
    wf = _workflow_id(invoice_id)
    done = store.get_step(conn, wf, "extract")
    if done:
        return json.loads(done["detail_json"])
    inv = store.get_invoice(conn, invoice_id)
    if inv is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    text = Path(inv["source_file"]).read_text(encoding="utf-8")
    fields = parse_invoice_text(text)
    conn.execute(
        """UPDATE invoices SET vendor=?, amount_paise=?, currency=?,
               invoice_date=?, due_date=?, description=?, status='EXTRACTED',
               extracted_json=?, updated_at=datetime('now')
           WHERE invoice_id=?""",
        (
            fields["vendor"],
            fields["amount_paise"],
            fields["currency"],
            fields["invoice_date"],
            fields["due_date"],
            fields["description"],
            json.dumps(fields),
            invoice_id,
        ),
    )
    conn.commit()
    store.record_step(conn, wf, "extract", fields)
    conn.close()
    return fields


def validate_invoice(db_path: str | Path, invoice_id: str) -> dict:
    """Step 2 — amount, vendor, duplicate check. Never pays on a guess."""
    conn = store.connect(db_path)
    wf = _workflow_id(invoice_id)
    done = store.get_step(conn, wf, "validate")
    if done:
        return json.loads(done["detail_json"])
    inv = store.get_invoice(conn, invoice_id)
    if inv is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    errors: list[str] = []
    amount = inv["amount_paise"]
    if amount is None or amount <= 0:
        errors.append("amount is missing or not positive")
    elif amount > MAX_AUTO_AMOUNT_PAISE:
        errors.append(
            f"amount {paise_to_rupees(amount)} exceeds auto-approval limit "
            f"{paise_to_rupees(MAX_AUTO_AMOUNT_PAISE)}"
        )
    if not inv["vendor"]:
        errors.append("vendor is missing")
    elif inv["vendor"] not in KNOWN_VENDORS:
        errors.append(f"unknown vendor: {inv['vendor']}")
    # Duplicate rule, written plainly: same vendor + same amount + same
    # date as another non-rejected invoice is a probable duplicate.
    dup = conn.execute(
        """SELECT invoice_id FROM invoices
           WHERE invoice_id != ? AND vendor = ? AND amount_paise = ?
             AND invoice_date = ? AND status != 'REJECTED' LIMIT 1""",
        (invoice_id, inv["vendor"], amount, inv["invoice_date"]),
    ).fetchone()
    if dup:
        errors.append(f"probable duplicate of {dup['invoice_id']}")
    result = {"valid": not errors, "errors": errors}
    if result["valid"]:
        conn.execute(
            "UPDATE invoices SET status='VALIDATED', updated_at=datetime('now')"
            " WHERE invoice_id=?",
            (invoice_id,),
        )
        conn.commit()
    store.record_step(conn, wf, "validate", result)
    conn.close()
    return result


def request_approval(
    db_path: str | Path, invoice_id: str, slack_url: str
) -> dict:
    """Step 3 — post the approval card to the mock Slack receiver.

    If a card was already posted for this workflow (e.g. activity retry
    after a crash), do not post a second card — return the cached result.
    """
    conn = store.connect(db_path)
    wf = _workflow_id(invoice_id)
    done = store.get_step(conn, wf, "request_approval")
    if done:
        return json.loads(done["detail_json"])
    inv = store.get_invoice(conn, invoice_id)
    if inv is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    payload = {
        "workflow_id": wf,
        "invoice_id": invoice_id,
        "vendor": inv["vendor"],
        "amount_paise": inv["amount_paise"],
        "amount_display": paise_to_rupees(inv["amount_paise"] or 0),
        "invoice_date": inv["invoice_date"],
        "due_date": inv["due_date"],
        "description": inv["description"],
        "approve_url": f"{slack_url.rstrip('/')}/approve/{wf}",
        "reject_url": f"{slack_url.rstrip('/')}/reject/{wf}",
    }
    req = urllib.request.Request(
        f"{slack_url.rstrip('/')}/slack/approval",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        posted = json.loads(resp.read() or b"{}")
    result = {"card_posted": True, "card": posted.get("card", payload)}
    conn.execute(
        "UPDATE invoices SET status='AWAITING_APPROVAL', updated_at=datetime('now')"
        " WHERE invoice_id=?",
        (invoice_id,),
    )
    conn.commit()
    store.record_step(conn, wf, "request_approval", result)
    conn.close()
    return result


def record_payment(db_path: str | Path, invoice_id: str) -> dict:
    """Step 4a — mark paid. Idempotency key = pay:<invoice>:<amount>.

    INSERT OR IGNORE + rowcount is the whole trick: the second call with
    the same key inserts zero rows, returns recorded=False, and the
    payments table still holds exactly one row for that key.
    """
    conn = store.connect(db_path)
    wf = _workflow_id(invoice_id)
    inv = store.get_invoice(conn, invoice_id)
    if inv is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    key = f"pay:{invoice_id}:{inv['amount_paise']}"
    cur = conn.execute(
        """INSERT OR IGNORE INTO payments
           (idempotency_key, workflow_id, invoice_id, amount_paise)
           VALUES (?, ?, ?, ?)""",
        (key, wf, invoice_id, inv["amount_paise"]),
    )
    conn.commit()
    recorded = cur.rowcount == 1
    if recorded:
        conn.execute(
            "UPDATE invoices SET status='PAID', reason=NULL,"
            " updated_at=datetime('now') WHERE invoice_id=?",
            (invoice_id,),
        )
        conn.commit()
    result = {
        "recorded": recorded,
        "idempotency_key": key,
        "amount_display": paise_to_rupees(inv["amount_paise"] or 0),
        "payments_for_key": store.count_payments(conn, key),
    }
    store.record_step(conn, wf, "payment", result)
    conn.close()
    return result


def record_rejection(db_path: str | Path, invoice_id: str, reason: str) -> dict:
    """Step 4b — mark rejected with the human's (or validator's) reason."""
    conn = store.connect(db_path)
    wf = _workflow_id(invoice_id)
    conn.execute(
        "UPDATE invoices SET status='REJECTED', reason=?,"
        " updated_at=datetime('now') WHERE invoice_id=?",
        (reason, invoice_id),
    )
    conn.commit()
    result = {"status": "REJECTED", "reason": reason}
    store.record_step(conn, wf, "rejection", result)
    conn.close()
    return result
