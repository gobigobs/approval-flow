"""Mock Slack approval receiver — stdlib only, no real Slack workspace.

What it does:
- POST /slack/approval  — workflow posts an approval card here (JSON).
- GET  /                — dashboard: every card, its status, Approve /
  Reject buttons (the thing the screenshot captures).
- POST /approve/<workflow_id> and /reject/<workflow_id> — the buttons'
  endpoints. They record the decision in the SQLite ledger and deliver
  it to the waiting workflow:
    * temporal mode: signal the Temporal workflow (real Signal API).
    * local mode:    the local runner polls the ledger, so recording
      the decision is the whole delivery.
- GET  /api/cards       — JSON for the demo script to poll.
- GET  /health          — liveness.

Run:  python -m approval_flow.mock_slack --mode temporal --port 18765
Env:  APPROVAL_FLOW_DB, TEMPORAL_ADDRESS
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import store
from .models import MOCK_SLACK_URL, TEMPORAL_ADDRESS

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "state.db"

DB_PATH = str(DEFAULT_DB)
MODE = "temporal"
TEMPORAL = os.environ.get("TEMPORAL_ADDRESS", TEMPORAL_ADDRESS)


def _signal_workflow(workflow_id: str, signal: str, reason: str) -> None:
    """Deliver a decision to a Temporal workflow via its Signal API."""
    from temporalio.client import Client

    async def _send() -> None:
        client = await Client.connect(TEMPORAL)
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(signal, reason)

    asyncio.run(_send())


def _card_html(card: dict, inv: dict | None) -> str:
    status = (inv or {}).get("status", "UNKNOWN")
    reason = (inv or {}).get("reason") or ""
    wf = card["workflow_id"]
    badge = {
        "AWAITING_APPROVAL": ("⏳ AWAITING APPROVAL", "#8a6d00", "#fff4cc"),
        "PAID": ("✅ PAID", "#0d7a3f", "#d9f2e3"),
        "REJECTED": ("❌ REJECTED", "#a11", "#fbdada"),
    }.get(status, (status, "#555", "#eee"))
    amount = card.get("amount_display", "?")
    decide = ""
    if status == "AWAITING_APPROVAL":
        decide = f"""
        <form method="post" action="/approve/{html.escape(wf)}" style="display:inline">
          <button class="ok" type="submit">✅ Approve</button>
        </form>
        <form method="post" action="/reject/{html.escape(wf)}" style="display:inline">
          <input name="reason" placeholder="Rejection reason" required>
          <button class="no" type="submit">❌ Reject</button>
        </form>"""
    reason_html = f"<p><b>Reason:</b> {html.escape(reason)}</p>" if reason else ""
    return f"""
    <div class="card">
      <div class="badge" style="color:{badge[1]};background:{badge[2]}">{badge[0]}</div>
      <h2>{html.escape(card.get('vendor', '?'))} — {html.escape(amount)}</h2>
      <p><b>Invoice:</b> {html.escape(card.get('invoice_id', '?'))}
         &nbsp;·&nbsp; <b>Date:</b> {html.escape(str(card.get('invoice_date', '')))}
         &nbsp;·&nbsp; <b>Due:</b> {html.escape(str(card.get('due_date', '')))}</p>
      <p>{html.escape(card.get('description') or '')}</p>
      {reason_html}
      <p class="meta">workflow <code>{html.escape(wf)}</code> · #finance-approvals (mock Slack)</p>
      {decide}
    </div>"""


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock Slack — #finance-approvals</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
  background:#f6f4ef;color:#1d1a16}}
header{{background:#1d1a16;color:#faf7f2;padding:18px 28px}}
header h1{{margin:0;font-size:20px}} header p{{margin:4px 0 0;color:#cbbfae;font-size:13px}}
main{{max-width:820px;margin:24px auto;padding:0 16px 40px}}
.card{{background:#fff;border:1px solid #e3ddd2;border-radius:14px;
  padding:20px 22px;margin-bottom:18px;box-shadow:0 2px 10px rgba(0,0,0,.06)}}
.badge{{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.04em;
  padding:4px 10px;border-radius:999px}}
.card h2{{margin:10px 0 6px;font-size:19px}} .card p{{margin:6px 0}}
.meta{{color:#8a8175;font-size:12px}}
button{{font-size:14px;padding:9px 16px;border-radius:9px;border:0;cursor:pointer;margin:6px 6px 0 0}}
.ok{{background:#0d7a3f;color:#fff}} .no{{background:#a11;color:#fff}}
input{{padding:9px 10px;border:1px solid #ccc;border-radius:9px;width:280px}}
.empty{{color:#8a8175;text-align:center;padding:40px 0}}
.note{{font-size:12px;color:#8a8175;text-align:center}}
</style></head><body>
<header><h1>💬 #finance-approvals <span style="font-weight:400;color:#cbbfae">(mock Slack)</span></h1>
<p>Approval cards posted by the invoice workflow. Approve / reject here — the decision is signalled back to the waiting workflow. Auto-refreshes every 5s.</p></header>
<main>{cards}<p class="note">Mock receiver only — no real Slack workspace, no tokens, nothing leaves this machine.</p></main>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep demo output clean
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, b'{"ok": true}', "application/json")
            return
        if self.path == "/api/cards":
            conn = store.connect(DB_PATH)
            rows = conn.execute(
                "SELECT * FROM cards ORDER BY created_at").fetchall()
            invs = {r["invoice_id"]: dict(r) for r in
                    conn.execute("SELECT * FROM invoices").fetchall()}
            conn.close()
            cards = []
            for r in rows:
                payload = json.loads(r["payload_json"])
                payload["status"] = invs.get(r["invoice_id"], {}).get(
                    "status", "UNKNOWN")
                cards.append(payload)
            self._send(200, json.dumps(cards).encode(), "application/json")
            return
        if self.path in ("/", "/index.html"):
            conn = store.connect(DB_PATH)
            rows = conn.execute(
                "SELECT * FROM cards ORDER BY created_at").fetchall()
            invs = {r["invoice_id"]: dict(r) for r in
                    conn.execute("SELECT * FROM invoices").fetchall()}
            conn.close()
            if not rows:
                cards_html = ('<p class="empty">No approval cards yet — '
                              "start a workflow and its card appears here.</p>")
            else:
                cards_html = "".join(
                    _card_html(json.loads(r["payload_json"]),
                               invs.get(r["invoice_id"]))
                    for r in rows)
            self._send(200, PAGE.format(cards=cards_html).encode())
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if self.path == "/slack/approval":
            payload = self._read_json()
            conn = store.connect(DB_PATH)
            conn.execute(
                """INSERT INTO cards (workflow_id, invoice_id, payload_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(workflow_id) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (payload["workflow_id"], payload["invoice_id"],
                 json.dumps(payload)),
            )
            conn.commit()
            conn.close()
            print(f"[mock-slack] card posted: {payload['invoice_id']} "
                  f"{payload.get('vendor')} {payload.get('amount_display')}",
                  flush=True)
            self._send(200, json.dumps(
                {"ok": True, "card": payload}).encode(), "application/json")
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in ("approve", "reject"):
            _, wf = parts
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                try:
                    data = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    data = {}
                reason = str(data.get("reason", ""))
            else:
                data = urllib.parse.parse_qs(raw.decode())
                reason = data.get("reason", [""])[0]
            decision = "approved" if parts[0] == "approve" else "rejected"
            if decision == "approved" and not reason:
                reason = "approved"
            conn = store.connect(DB_PATH)
            store.save_decision(conn, wf, decision, reason)
            row = conn.execute(
                "SELECT invoice_id FROM cards WHERE workflow_id = ?",
                (wf,)).fetchone()
            conn.close()
            invoice_id = row["invoice_id"] if row else wf
            print(f"[mock-slack] {decision.upper()}: {invoice_id} "
                  f"({reason})", flush=True)
            if MODE == "temporal":
                try:
                    _signal_workflow(wf, parts[0], reason)
                except Exception as exc:  # noqa: BLE001 — report, don't hang
                    print(f"[mock-slack] signal failed: {exc}", flush=True)
                    self._send(502, b"workflow signal failed", "text/plain")
                    return
            accept = self.headers.get("Accept", "")
            if "application/json" in accept or "application/json" in ctype:
                self._send(200, json.dumps(
                    {"ok": True, "decision": decision,
                     "workflow_id": wf}).encode(), "application/json")
            else:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        self._send(404, b"not found", "text/plain")


def main() -> None:
    global DB_PATH, MODE
    ap = argparse.ArgumentParser(description="Mock Slack approval receiver")
    ap.add_argument("--mode", choices=["temporal", "local"],
                    default="temporal")
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--db", default=os.environ.get(
        "APPROVAL_FLOW_DB", str(DEFAULT_DB)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    DB_PATH = args.db
    MODE = args.mode
    store.connect(DB_PATH).close()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[mock-slack] listening on http://{args.host}:{args.port}/ "
          f"(mode={MODE})", flush=True)
    print(f"[mock-slack] dashboard: http://{args.host}:{args.port}/",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
