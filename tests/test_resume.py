"""Resume: a run killed mid-flight resumes instead of restarting.

Simulates the crash the demo does for real: run the local runner far
enough that extract/validate/card are done, pre-record the human
decision (as the mock Slack server would), then run the runner again
and assert extract still ran exactly once — i.e. the second run resumed
after the completed steps instead of re-extracting.
"""

from __future__ import annotations

from approval_flow import local_runner, steps, store


def _seed(tmp_path, invoice_id="INV-R1"):
    src = tmp_path / f"{invoice_id}.txt"
    src.write_text(
        f"INVOICE\nInvoice No: {invoice_id}\nVendor: All Food Supplies\n"
        f"Date: 2026-11-20\nCurrency: INR\nAmount: 1000.00\n"
        f"Due Date: 2026-11-29\nDescription: test\n")
    db = tmp_path / "test.db"
    conn = store.connect(db)
    conn.execute(
        "INSERT INTO invoices (invoice_id, status, source_file) VALUES (?, 'SEEDED', ?)",
        (invoice_id, str(src)),
    )
    conn.commit()
    conn.close()
    return db


def test_resume_skips_completed_steps(tmp_path):
    db = _seed(tmp_path)
    # First run: do the work up to the approval wait, then "crash".
    steps.extract_invoice(db, "INV-R1")
    steps.validate_invoice(db, "INV-R1")
    # Card post needs a server; record the step directly to simulate a
    # card that was already posted before the crash.
    conn = store.connect(db)
    store.record_step(conn, "invoice-INV-R1", "request_approval",
                      {"card_posted": True})
    store.save_decision(conn, "invoice-INV-R1", "approved", "approved")
    conn.close()

    # Second run (the "restarted" runner) must resume, not restart.
    result = local_runner.run_invoice(db, "INV-R1", "http://127.0.0.1:9",
                                      timeout=30)
    assert result["status"] == "PAID"
    conn = store.connect(db)
    extract = store.get_step(conn, "invoice-INV-R1", "extract")
    assert extract["runs"] == 1, "extract re-ran after resume"
    assert store.count_payments(conn) == 1
    conn.close()


def test_completed_invoice_is_noop_on_rerun(tmp_path):
    db = _seed(tmp_path, "INV-R2")
    conn = store.connect(db)
    # Simulate a run that already posted its card before the "crash".
    store.record_step(conn, "invoice-INV-R2", "request_approval",
                      {"card_posted": True})
    store.save_decision(conn, "invoice-INV-R2", "approved", "approved")
    conn.close()
    first = local_runner.run_invoice(db, "INV-R2", "http://127.0.0.1:9",
                                     timeout=30)
    second = local_runner.run_invoice(db, "INV-R2", "http://127.0.0.1:9",
                                      timeout=30)
    assert first["status"] == "PAID"
    assert second["status"] == "PAID"
    assert second.get("resumed") is True
    conn = store.connect(db)
    assert store.count_payments(conn) == 1
    conn.close()
