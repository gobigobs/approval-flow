"""Validation rules: amount, vendor, duplicate check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from approval_flow import steps, store

INVOICE_TEXT = """INVOICE
Invoice No: {inv}
Vendor: {vendor}
Date: {date}
Currency: INR
Amount: {amount}
Due Date: 2026-11-29
Description: test
"""


def _seed_invoice(tmp_path, invoice_id, vendor, amount, date="2026-11-20"):
    src = tmp_path / f"{invoice_id}.txt"
    src.write_text(INVOICE_TEXT.format(
        inv=invoice_id, vendor=vendor, amount=amount, date=date))
    db = tmp_path / "test.db"
    conn = store.connect(db)
    conn.execute(
        "INSERT INTO invoices (invoice_id, status, source_file) VALUES (?, 'SEEDED', ?)",
        (invoice_id, str(src)),
    )
    conn.commit()
    conn.close()
    return db


def test_valid_invoice_passes(tmp_path):
    db = _seed_invoice(tmp_path, "INV-T1", "Varagi Trades Hyper", "100.00")
    steps.extract_invoice(db, "INV-T1")
    assert steps.validate_invoice(db, "INV-T1") == {"valid": True, "errors": []}


def test_unknown_vendor_fails(tmp_path):
    db = _seed_invoice(tmp_path, "INV-T2", "Mystery Traders", "100.00")
    steps.extract_invoice(db, "INV-T2")
    result = steps.validate_invoice(db, "INV-T2")
    assert result["valid"] is False
    assert any("unknown vendor" in e for e in result["errors"])


def test_zero_amount_fails(tmp_path):
    db = _seed_invoice(tmp_path, "INV-T3", "Savitha Mart", "0.00")
    steps.extract_invoice(db, "INV-T3")
    result = steps.validate_invoice(db, "INV-T3")
    assert result["valid"] is False
    assert any("amount" in e for e in result["errors"])


def test_huge_amount_fails(tmp_path):
    db = _seed_invoice(tmp_path, "INV-T4", "Savitha Mart", "150000.00")
    steps.extract_invoice(db, "INV-T4")
    result = steps.validate_invoice(db, "INV-T4")
    assert result["valid"] is False
    assert any("exceeds" in e for e in result["errors"])


def test_duplicate_same_vendor_amount_date_fails(tmp_path):
    db = _seed_invoice(tmp_path, "INV-T5", "Savitha Mart", "500.00")
    steps.extract_invoice(db, "INV-T5")
    steps.validate_invoice(db, "INV-T5")
    db2 = _seed_invoice(tmp_path, "INV-T6", "Savitha Mart", "500.00")
    assert db2 == db  # same tmp db file
    steps.extract_invoice(db, "INV-T6")
    result = steps.validate_invoice(db, "INV-T6")
    assert result["valid"] is False
    assert any("duplicate" in e for e in result["errors"])


def test_extract_missing_field_raises(tmp_path):
    src = tmp_path / "INV-T7.txt"
    src.write_text("INVOICE\nVendor: Savitha Mart\n")  # no amount, no date
    db = tmp_path / "test.db"
    conn = store.connect(db)
    conn.execute(
        "INSERT INTO invoices (invoice_id, status, source_file) VALUES ('INV-T7', 'SEEDED', ?)",
        (str(src),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="could not extract"):
        steps.extract_invoice(db, "INV-T7")
