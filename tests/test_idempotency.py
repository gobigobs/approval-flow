"""Idempotency: retrying the payment step must never double-pay.

The proof is the payments table, not a mock: call record_payment twice
with the same invoice (same deterministic key pay:<invoice>:<amount>)
and assert exactly one payment row exists and the second call reports
recorded=False.
"""

from __future__ import annotations

from approval_flow import steps, store


def _seed_paid_invoice(tmp_path, invoice_id="INV-P1", amount="250.00"):
    src = tmp_path / f"{invoice_id}.txt"
    src.write_text(
        f"INVOICE\nInvoice No: {invoice_id}\nVendor: Savitha Mart\n"
        f"Date: 2026-11-20\nCurrency: INR\nAmount: {amount}\n"
        f"Due Date: 2026-11-29\nDescription: test\n")
    db = tmp_path / "test.db"
    conn = store.connect(db)
    conn.execute(
        "INSERT INTO invoices (invoice_id, status, source_file) VALUES (?, 'SEEDED', ?)",
        (invoice_id, str(src)),
    )
    conn.commit()
    conn.close()
    steps.extract_invoice(db, invoice_id)
    return db


def test_payment_retry_records_once(tmp_path):
    db = _seed_paid_invoice(tmp_path)
    first = steps.record_payment(db, "INV-P1")
    second = steps.record_payment(db, "INV-P1")
    assert first["recorded"] is True
    assert second["recorded"] is False
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["idempotency_key"] == "pay:INV-P1:25000"
    conn = store.connect(db)
    assert store.count_payments(conn, "pay:INV-P1:25000") == 1
    assert store.count_payments(conn) == 1
    assert store.get_invoice(conn, "INV-P1")["status"] == "PAID"
    conn.close()


def test_different_invoices_get_different_keys(tmp_path):
    db = _seed_paid_invoice(tmp_path, "INV-P2", "100.00")
    steps.extract_invoice(db, "INV-P2")  # cached, no-op
    db2 = _seed_paid_invoice(tmp_path, "INV-P3", "100.00")
    assert db2 == db
    steps.record_payment(db, "INV-P2")
    steps.record_payment(db, "INV-P3")
    conn = store.connect(db)
    assert store.count_payments(conn) == 2
    conn.close()
