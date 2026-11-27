"""Seed the three demo invoices into the SQLite ledger.

Deterministic: re-running seed on a fresh DB always produces the same
three invoices. The invoice *fields* are NOT seeded — the workflow's
extract step parses them from the text files, so the demo really
exercises extraction.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import store

INVOICE_IDS = ["INV-2026-1042", "INV-2026-1043", "INV-2026-1044"]


def seed(db_path: str | Path, invoices_dir: str | Path) -> list[str]:
    conn = store.connect(db_path)
    seeded: list[str] = []
    for invoice_id in INVOICE_IDS:
        source = Path(invoices_dir) / f"{invoice_id}.txt"
        if not source.exists():
            raise FileNotFoundError(f"seed invoice missing: {source}")
        conn.execute(
            """INSERT OR REPLACE INTO invoices
               (invoice_id, status, source_file)
               VALUES (?, 'SEEDED', ?)""",
            (invoice_id, str(source)),
        )
        seeded.append(invoice_id)
    conn.commit()
    conn.close()
    return seeded


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed demo invoices")
    ap.add_argument("--db", required=True)
    ap.add_argument("--invoices-dir", required=True)
    args = ap.parse_args()
    for invoice_id in seed(args.db, args.invoices_dir):
        print(f"seeded {invoice_id}")


if __name__ == "__main__":
    main()
