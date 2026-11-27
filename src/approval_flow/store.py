"""SQLite ledger: the durable record every step writes to.

Two durability layers exist in this project, on purpose:

1. Temporal's own event history (real Temporal mode) — the workflow can be
   replayed from history after a worker restart.
2. This SQLite ledger — an independent, human-readable record of which step
   completed, how many times each step ran, which payments were recorded
   (guarded by idempotency key), and what each human decided.

The local runner uses layer 2 alone; the Temporal path uses both, and the
demo cross-checks them against each other.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   TEXT PRIMARY KEY,
    vendor       TEXT,
    amount_paise INTEGER,
    currency     TEXT DEFAULT 'INR',
    invoice_date TEXT,
    due_date     TEXT,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'SEEDED',
    reason       TEXT,
    extracted_json TEXT,
    source_file  TEXT,
    updated_at   TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS steps (
    workflow_id  TEXT NOT NULL,
    step         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'completed',
    runs         INTEGER NOT NULL DEFAULT 1,
    detail_json  TEXT,
    completed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (workflow_id, step)
);
CREATE TABLE IF NOT EXISTS payments (
    idempotency_key TEXT PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    invoice_id   TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    recorded_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS decisions (
    workflow_id  TEXT PRIMARY KEY,
    decision     TEXT NOT NULL,
    reason       TEXT,
    decided_by   TEXT DEFAULT 'finance-approver',
    decided_at   TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cards (
    workflow_id  TEXT PRIMARY KEY,
    invoice_id   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_invoice(conn: sqlite3.Connection, invoice_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
    ).fetchone()
    return dict(row) if row else None


def list_invoices(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM invoices ORDER BY invoice_id").fetchall()
    return [dict(r) for r in rows]


def get_step(conn: sqlite3.Connection, workflow_id: str, step: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM steps WHERE workflow_id = ? AND step = ?",
        (workflow_id, step),
    ).fetchone()
    return dict(row) if row else None


def record_step(
    conn: sqlite3.Connection, workflow_id: str, step: str, detail: dict
) -> dict:
    """Mark a step completed. Re-running a completed step bumps `runs` but
    never rewrites the first completion time — that counter is the resume
    proof: after a worker restart, extract must still show runs == 1."""
    conn.execute(
        """INSERT INTO steps (workflow_id, step, status, runs, detail_json)
           VALUES (?, ?, 'completed', 1, ?)
           ON CONFLICT(workflow_id, step) DO UPDATE SET
             runs = steps.runs + 1,
             detail_json = excluded.detail_json""",
        (workflow_id, step, json.dumps(detail)),
    )
    conn.commit()
    return get_step(conn, workflow_id, step)  # type: ignore[return-value]


def get_decision(conn: sqlite3.Connection, workflow_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM decisions WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    return dict(row) if row else None


def save_decision(
    conn: sqlite3.Connection,
    workflow_id: str,
    decision: str,
    reason: str = "",
    decided_by: str = "finance-approver",
) -> None:
    conn.execute(
        """INSERT INTO decisions (workflow_id, decision, reason, decided_by)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(workflow_id) DO UPDATE SET
             decision = excluded.decision, reason = excluded.reason,
             decided_by = excluded.decided_by,
             decided_at = datetime('now')""",
        (workflow_id, decision, reason, decided_by),
    )
    conn.commit()


def count_payments(conn: sqlite3.Connection, idempotency_key: str | None = None) -> int:
    if idempotency_key:
        row = conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) c FROM payments").fetchone()
    return int(row["c"])
