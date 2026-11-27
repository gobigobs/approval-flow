"""Temporal worker: hosts the workflow + activities on the task queue.

Run:  python -m approval_flow.worker
Env:  TEMPORAL_ADDRESS (default localhost:7233)
      APPROVAL_FLOW_DB (default <repo>/data/state.db)
      MOCK_SLACK_URL   (default http://127.0.0.1:18765)
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import ALL_ACTIVITIES
from .models import TASK_QUEUE, TEMPORAL_ADDRESS
from .workflows import InvoiceApprovalWorkflow


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", TEMPORAL_ADDRESS)
    client = await Client.connect(address)
    print(f"[worker] connected to Temporal at {address}, queue={TASK_QUEUE}",
          flush=True)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[InvoiceApprovalWorkflow],
        activities=ALL_ACTIVITIES,
        # Activities are sync defs (SQLite + urllib): they need a thread
        # pool or the worker refuses to start. Without this the error is:
        # "Activity ... is not async so an activity_executor must be present".
        activity_executor=ThreadPoolExecutor(max_workers=10),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
