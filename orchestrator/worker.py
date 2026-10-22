"""Temporal Worker (Lite-версия).

Регистрирует ТОЛЬКО EnterpriseHITLWorkflow и базовые активности:
  - execute_openhands_phase
  - prepare_workspace
  - cleanup_workspace
  - git_push_and_create_pr
"""

import asyncio
import logging
import os
import random
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from activities import (
    cleanup_workspace,
    ensure_runs_dir_writable,
    execute_openhands_phase,
    git_push_and_create_pr,
    prepare_workspace,
)
from workflows import EnterpriseHITLWorkflow

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "temporal:7233")

MAX_RETRIES = int(os.getenv("TEMPORAL_CONNECT_MAX_RETRIES", "15"))
BASE_DELAY = float(os.getenv("TEMPORAL_CONNECT_BASE_DELAY", "1"))
MAX_DELAY = float(os.getenv("TEMPORAL_CONNECT_MAX_DELAY", "60"))

ACTIVITY_MAX_WORKERS = int(os.getenv("TEMPORAL_ACTIVITY_MAX_WORKERS", "10"))
MAX_CONCURRENT_ACTIVITIES = int(os.getenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "4"))
MAX_CACHED_WORKFLOWS = int(os.getenv("TEMPORAL_MAX_CACHED_WORKFLOWS", "200"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("temporal-worker")


def _backoff_delay(attempt: int) -> float:
    """Экспоненциальный backoff с jitter (±10%)."""
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    return delay * random.uniform(0.9, 1.1)


async def connect_to_temporal() -> Client:
    """Подключение к Temporal Server с экспоненциальным backoff."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = await Client.connect(TEMPORAL_HOST)
            logger.info("Connected to Temporal server at %s", TEMPORAL_HOST)
            return client
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < MAX_RETRIES:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Temporal connection failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, MAX_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)
    raise RuntimeError(f"Could not connect to Temporal after {MAX_RETRIES} attempts") from last_error


async def main():
    logger.info("Temporal Worker starting — target: %s", TEMPORAL_HOST)
    logger.info("Ensuring workspace runs dir is writable: %s", ensure_runs_dir_writable())

    client = await connect_to_temporal()

    activity_executor = ThreadPoolExecutor(max_workers=ACTIVITY_MAX_WORKERS)

    worker = Worker(
        client,
        task_queue="agent-tasks-queue",
        workflows=[EnterpriseHITLWorkflow],
        activities=[
            execute_openhands_phase,
            prepare_workspace,
            cleanup_workspace,
            git_push_and_create_pr,
        ],
        activity_executor=activity_executor,
        max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
        max_cached_workflows=MAX_CACHED_WORKFLOWS,
    )

    logger.info(
        "Worker is ready (ThreadPoolExecutor: %d threads, max_cached_workflows=%d). "
        "Listening on task queue 'agent-tasks-queue'...",
        ACTIVITY_MAX_WORKERS,
        MAX_CACHED_WORKFLOWS,
    )
    try:
        await worker.run()
    finally:
        activity_executor.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
