#!/usr/bin/env python3
"""
Entry point for the Temporal worker that runs CodeFixVerificationWorkflow.

Run with: python -m sre_agent.sandbox_worker

A dedicated, long-lived process — separate from the API's request/response
lifecycle — that polls the sandbox task queue and executes the code-fix
verification pipeline (sre_agent.sandbox_workflow). Reuses the sentinel/api
image in deployment (see deploy/helm/sentinel/templates/temporal-worker.yaml)
but is a distinct running process with a distinct job.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _main() -> None:
    from temporalio.worker import Worker

    from .sandbox_workflow import ACTIVITIES, CodeFixVerificationWorkflow
    from .temporal_client import get_temporal_client, task_queue, temporal_enabled

    if not temporal_enabled():
        logger.warning("TEMPORAL_ENABLED is false; sandbox_worker has nothing to do. Exiting.")
        return

    client = await get_temporal_client()
    if client is None:
        raise RuntimeError("TEMPORAL_ENABLED is true but no Temporal client could be constructed")

    queue = task_queue()
    logger.info("Starting sandbox Temporal worker on task queue %r", queue)
    worker = Worker(
        client,
        task_queue=queue,
        workflows=[CodeFixVerificationWorkflow],
        activities=ACTIVITIES,
    )
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    asyncio.run(_main())


if __name__ == "__main__":
    main()
