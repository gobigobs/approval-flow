"""End-to-end demo orchestrator (run via ./run.sh, not by hand).

Temporal mode:
  1. seeds 3 invoices, starts the mock Slack server + Temporal worker
  2. starts 3 workflows; waits until all 3 approval cards are posted
  3. screenshots the pending cards
  4. KILLS the worker mid-run (all 3 workflows are awaiting approval),
     restarts it, and proves the INV-2026-1044 workflow is still RUNNING
     with extract executed exactly once
  5. approves INV-2026-1042, rejects INV-2026-1043 with a reason,
     approves INV-2026-1044 — all through the mock Slack buttons' endpoints
  6. replays the INV-2026-1042 payment with the same idempotency key and
     proves the payments table still holds exactly one row for it
  7. runs the pytest idempotency test live, screenshots the final
     dashboard, prints the final table + writes docs/demo-summary.json

Local mode (--mode local): same script, but the three invoices run under
local_runner subprocesses instead of Temporal; the 1044 runner is killed
and restarted instead of the worker. Same assertions, same proofs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from approval_flow import seed as seed_mod  # noqa: E402
from approval_flow import steps, store  # noqa: E402
from approval_flow.models import MOCK_SLACK_URL, TASK_QUEUE  # noqa: E402

INVOICES = ["INV-2026-1042", "INV-2026-1043", "INV-2026-1044"]
SLACK_URL = MOCK_SLACK_URL
REJECT_REASON = "Duplicate of last month's order — verify with Savitha Mart before paying"


def log(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def _log_file(name: str, mode: str = "w"):
    """Child-process output goes to a file, never a PIPE: a crashed
    worker's traceback must survive to be read, and a PIPE nobody
    drains can block the child once its buffer fills."""
    path = ROOT / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, mode, encoding="utf-8")


def _dump_log_tail(name: str, lines: int = 25) -> None:
    path = ROOT / "data" / name
    if not path.exists():
        return
    tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    if tail:
        print(f"--- tail of data/{name} ---")
        print("\n".join(tail))


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read() or b"{}")


def wait_for(url: str, timeout: int = 60, what: str = "service") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            http_json("GET", url)
            return
        except Exception:  # noqa: BLE001 — not up yet, keep waiting
            time.sleep(0.5)
    raise TimeoutError(f"{what} did not come up at {url} within {timeout}s")


def wait_for_cards(n: int, timeout: int = 90) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cards = http_json("GET", f"{SLACK_URL}/api/cards")
        except Exception:  # noqa: BLE001
            cards = []
        if len(cards) >= n:
            return cards
        time.sleep(0.5)
    raise TimeoutError(f"only {len(cards)} approval cards appeared, wanted {n}")


def screenshot(out: Path, wait_text: str) -> None:
    script = ROOT / "docs" / "capture.mjs"
    if not script.exists():
        log("capture script missing — skipping screenshot")
        return
    proc = subprocess.run(
        ["node", str(script), "--url", SLACK_URL + "/",
         "--out", str(out), "--wait-text", wait_text],
        capture_output=True, text=True, timeout=120)
    if proc.returncode == 0:
        log(f"screenshot saved: {out}")
    else:
        log(f"screenshot FAILED (continuing): {proc.stdout.strip()} "
            f"{proc.stderr.strip()[-300:]}")


def ledger_counts(db: Path) -> dict:
    conn = store.connect(db)
    out = {}
    for inv in INVOICES:
        wf = f"invoice-{inv}"
        out[inv] = {
            "status": (store.get_invoice(conn, inv) or {}).get("status"),
            "extract_runs": (store.get_step(conn, wf, "extract") or {}).get("runs"),
            "payment_runs": (store.get_step(conn, wf, "payment") or {}).get("runs"),
        }
    out["payments_total"] = store.count_payments(conn)
    conn.close()
    return out


# ---------------------------------------------------------------- temporal
async def _temporal_start_all() -> None:
    """Fresh client per asyncio.run: a temporalio Client must not be
    reused across event loops, so every async block connects anew."""
    from temporalio.client import Client
    from approval_flow.workflows import InvoiceApprovalWorkflow

    client = await Client.connect("localhost:7233")
    for inv in INVOICES:
        await client.start_workflow(
            InvoiceApprovalWorkflow.run, inv,
            id=f"invoice-{inv}", task_queue=TASK_QUEUE)
        log(f"workflow started: invoice-{inv}")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        ready = 0
        for inv in INVOICES:
            handle = client.get_workflow_handle(f"invoice-{inv}")
            try:
                status = await handle.query(InvoiceApprovalWorkflow.status)
                if status["step"] == "awaiting_approval":
                    ready += 1
            except Exception:  # noqa: BLE001 — not queryable yet
                pass
        if ready == len(INVOICES):
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("workflows did not all reach awaiting_approval")


async def _temporal_describe_running(invoice_id: str) -> str:
    from temporalio.client import Client, WorkflowExecutionStatus

    client = await Client.connect("localhost:7233")
    handle = client.get_workflow_handle(f"invoice-{invoice_id}")
    desc = await handle.describe()
    assert desc.status == WorkflowExecutionStatus.RUNNING, (
        f"expected RUNNING, got {desc.status!r}")
    return getattr(desc.status, "name", str(desc.status))


async def _temporal_results(timeout: int = 180) -> dict:
    from temporalio.client import Client

    client = await Client.connect("localhost:7233")
    results = {}

    async def _one(inv: str):
        handle = client.get_workflow_handle(f"invoice-{inv}")
        results[inv] = await asyncio.wait_for(handle.result(), timeout=timeout)

    await asyncio.gather(*[_one(inv) for inv in INVOICES])
    return results


def run_temporal_demo(db: Path, env: dict) -> subprocess.Popen:
    worker = subprocess.Popen(
        [sys.executable, "-m", "approval_flow.worker"],
        env=env, stdout=_log_file("worker.log"),
        stderr=subprocess.STDOUT)
    log("worker started (pid %d)" % worker.pid)
    time.sleep(3)
    if worker.poll() is not None:
        _dump_log_tail("worker.log")
        raise RuntimeError("worker exited early")
    asyncio.run(_temporal_start_all())
    log("all 3 workflows are awaiting approval")
    return worker


def kill_and_restart_worker(worker, env) -> subprocess.Popen:
    log("KILLING worker mid-run (SIGTERM) — workflows are awaiting approval...")
    worker.terminate()
    worker.wait(timeout=10)
    log(f"worker dead (exit {worker.returncode}); restarting...")
    new = subprocess.Popen(
        [sys.executable, "-m", "approval_flow.worker"],
        env=env, stdout=_log_file("worker.log", "a"),
        stderr=subprocess.STDOUT)
    time.sleep(4)
    if new.poll() is not None:
        _dump_log_tail("worker.log")
        raise RuntimeError("restarted worker exited early")
    log(f"worker restarted (pid {new.pid})")
    return new


# ------------------------------------------------------------------- local
def run_local_demo(db: Path, env: dict) -> dict[str, subprocess.Popen]:
    runners: dict[str, subprocess.Popen] = {}
    for inv in INVOICES:
        runners[inv] = subprocess.Popen(
            [sys.executable, "-m", "approval_flow.local_runner",
             "--invoice", inv],
            env=env, stdout=_log_file(f"runner-{inv}.log"),
            stderr=subprocess.STDOUT)
        log(f"local runner started for {inv} (pid {runners[inv].pid})")
    return runners


def kill_and_restart_runner(runners, invoice_id, env) -> None:
    proc = runners[invoice_id]
    log(f"KILLING local runner for {invoice_id} mid-run (SIGTERM)...")
    proc.terminate()
    proc.wait(timeout=10)
    log(f"runner dead (exit {proc.returncode}); restarting...")
    runners[invoice_id] = subprocess.Popen(
        [sys.executable, "-m", "approval_flow.local_runner",
         "--invoice", invoice_id],
        env=env, stdout=_log_file(f"runner-{invoice_id}.log", "a"),
        stderr=subprocess.STDOUT)
    time.sleep(1)
    log(f"runner restarted (pid {runners[invoice_id].pid})")


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="approval-flow end-to-end demo")
    ap.add_argument("--mode", choices=["temporal", "local"], default="temporal")
    ap.add_argument("--db", default=str(ROOT / "data" / "state.db"))
    args = ap.parse_args()
    db = Path(args.db)
    mode = args.mode

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["APPROVAL_FLOW_DB"] = str(db)
    env["MOCK_SLACK_URL"] = SLACK_URL
    env["TEMPORAL_ADDRESS"] = "localhost:7233"

    # Fresh state every demo run (deterministic).
    for suffix in ("", "-shm", "-wal"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    seeded = seed_mod.seed(db, ROOT / "data" / "invoices")
    log(f"seeded invoices: {', '.join(seeded)}")

    slack = subprocess.Popen(
        [sys.executable, "-m", "approval_flow.mock_slack",
         "--mode", mode, "--port", "18765", "--db", str(db)],
        env=env, stdout=_log_file("mock-slack.log"),
        stderr=subprocess.STDOUT)
    procs = [slack]
    try:
        wait_for(f"{SLACK_URL}/health", 30, "mock Slack")
        log(f"mock Slack up at {SLACK_URL}/ (mode={mode})")

        if mode == "temporal":
            worker = run_temporal_demo(db, env)
            procs.append(worker)
        else:
            runners = run_local_demo(db, env)
            procs.extend(runners.values())

        wait_for_cards(3)
        log("all 3 approval cards posted to #finance-approvals")
        screenshot(ROOT / "docs" / "demo-approval-card.png",
                   "AWAITING APPROVAL")

        # ---- restart-resume proof ----
        if mode == "temporal":
            worker = kill_and_restart_worker(worker, env)
            procs[-1] = worker
            status = asyncio.run(_temporal_describe_running("INV-2026-1044"))
            log(f"after restart: invoice-INV-2026-1044 workflow status = "
                f"{status} (still running, not restarted)")
            assert "RUNNING" in status, f"expected RUNNING, got {status}"
        else:
            kill_and_restart_runner(runners, "INV-2026-1044", env)
            procs.extend(runners.values())
        counts = ledger_counts(db)
        runs_1044 = counts["INV-2026-1044"]["extract_runs"]
        log(f"ledger after restart: INV-2026-1044 extract runs = {runs_1044} "
            "(1 = resumed, not re-extracted)")
        assert runs_1044 == 1, f"extract re-ran after restart: {runs_1044}"

        # ---- human decisions, via the same endpoints the card buttons use ----
        http_json("POST", f"{SLACK_URL}/approve/invoice-INV-2026-1042",
                  {"reason": "approved"})
        log("approved INV-2026-1042 (Varagi Trades Hyper, Rs 18,450.00)")
        http_json("POST", f"{SLACK_URL}/reject/invoice-INV-2026-1043",
                  {"reason": REJECT_REASON})
        log("rejected INV-2026-1043 (Savitha Mart, Rs 7,290.00)")
        http_json("POST", f"{SLACK_URL}/approve/invoice-INV-2026-1044",
                  {"reason": "approved"})
        log("approved INV-2026-1044 (All Food Supplies, Rs 52,300.00)")

        # ---- wait for terminal states ----
        if mode == "temporal":
            results = asyncio.run(_temporal_results())
            for inv, res in results.items():
                log(f"workflow {inv} -> {res['status']}")
        else:
            for inv, proc in runners.items():
                proc.wait(timeout=180)
                log(f"local runner {inv} exited {proc.returncode}")
        time.sleep(1)

        # ---- idempotency live replay ----
        replay = steps.record_payment(db, "INV-2026-1042")
        log(f"payment replay for INV-2026-1042 with key "
            f"{replay['idempotency_key']}: recorded={replay['recorded']}, "
            f"payments for key = {replay['payments_for_key']} (must be 1)")
        assert replay["recorded"] is False
        assert replay["payments_for_key"] == 1

        # ---- pytest idempotency test, run live ----
        pytest = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_idempotency.py", "-q"],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
        log("pytest tests/test_idempotency.py:\n" + pytest.stdout.strip())
        if pytest.returncode != 0:
            log("pytest FAILED:\n" + pytest.stderr.strip()[-1000:])
            return 1

        screenshot(ROOT / "docs" / "demo-final.png", "PAID")

        # ---- final assertions ----
        counts = ledger_counts(db)
        conn = store.connect(db)
        final = {r["invoice_id"]: r for r in store.list_invoices(conn)}
        conn.close()
        assert final["INV-2026-1042"]["status"] == "PAID"
        assert final["INV-2026-1043"]["status"] == "REJECTED"
        assert REJECT_REASON in (final["INV-2026-1043"]["reason"] or "")
        assert final["INV-2026-1044"]["status"] == "PAID"
        assert counts["payments_total"] == 2
        for inv in INVOICES:
            assert counts[inv]["extract_runs"] == 1, \
                f"{inv} extract runs = {counts[inv]['extract_runs']}"

        summary = {
            "mode": mode,
            "invoices": {
                inv: {
                    "status": final[inv]["status"],
                    "vendor": final[inv]["vendor"],
                    "amount_paise": final[inv]["amount_paise"],
                    "reason": final[inv]["reason"],
                    "extract_runs": counts[inv]["extract_runs"],
                } for inv in INVOICES
            },
            "payments_total": counts["payments_total"],
            "idempotency_replay": replay,
            "restart_resume": {
                "killed_mid_run": "worker" if mode == "temporal"
                                  else "local-runner:INV-2026-1044",
                "extract_runs_after_restart_INV-2026-1044": runs_1044,
            },
        }
        (ROOT / "docs" / "demo-summary.json").write_text(
            json.dumps(summary, indent=2))
        print()
        print("=" * 64)
        print("FINAL STATE")
        print("=" * 64)
        for inv in INVOICES:
            r = final[inv]
            line = f"  {inv}  {r['vendor']:<22} Rs {r['amount_paise']/100:>10,.2f}  {r['status']}"
            if r["reason"]:
                line += f"  ({r['reason']})"
            print(line)
        print(f"  payments recorded: {counts['payments_total']} "
              "(INV-2026-1042 + INV-2026-1044; the replay added none)")
        print(f"  extract runs per invoice: 1 (resume worked, nothing re-ran)")
        print("=" * 64)
        print(f"DEMO PASSED ({mode} mode). Summary: docs/demo-summary.json")
        return 0
    except Exception:
        print("[demo] FAILED — child logs:")
        for name in ["mock-slack.log", "worker.log"] + \
                [f"runner-{inv}.log" for inv in INVOICES]:
            _dump_log_tail(name)
        raise
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        log("demo processes stopped")


if __name__ == "__main__":
    raise SystemExit(main())
