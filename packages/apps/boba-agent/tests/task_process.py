"""Процесс независимого worker для интеграционных проверок."""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import NonNegativeFloat

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspace
from boba.tasks.models import (
    ExecutionContext,
    TaskExecutor,
    TaskLease,
    TaskModel,
    TaskOutcome,
    TaskState,
    TaskStatus,
    WorkerConfig,
    WorkerIdentity,
)
from boba.tasks.service import TaskService
from boba.tasks.worker import TaskWorker


@dataclass(frozen=True)
class ProcessConfig:
    postgres: PostgresConfig
    krb5_config: Path
    cache_dir: Path


class ProcessPayload(TaskModel):
    delay: NonNegativeFloat


class ProcessExecutor(TaskExecutor):
    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)

    async def execute(self, task: TaskState, context: ExecutionContext) -> TaskOutcome:
        payload = ProcessPayload.model_validate(task.payload)
        self.logger.info(
            "Task %s: running for %s seconds", task.task_id, payload.delay
        )

        await context.report_progress({"pid": os.getpid()})
        await asyncio.sleep(payload.delay)
        self.logger.info("Task %s: executor completed", task.task_id)

        return TaskOutcome(status=TaskStatus.COMPLETED, result={"pid": os.getpid()})


class ClaimPausedService(TaskService):
    """Останавливает тестовый процесс после commit захвата, до входа в исполнитель."""

    async def claim(
        self, worker: WorkerIdentity, token: UUID, lease_sec: float
    ) -> TaskLease | None:
        lease = await super().claim(worker, token, lease_sec)
        if lease is not None:
            os.kill(os.getpid(), signal.SIGSTOP)

        return lease


class ProcessWorker:
    async def run(
        self,
        postgres: PostgresConfig,
        owner: str,
        worker_id: str,
        maintenance: bool,
        pause_after_claim: bool,
    ) -> None:
        pool = AsyncPostgresPool(postgres)
        await pool.open()
        try:
            service = TaskService(pool)
            if pause_after_claim:
                service = ClaimPausedService(pool)

            worker = TaskWorker(
                service,
                WorkerIdentity(
                    worker_id, frozenset({"process"}), owners=frozenset({owner})
                ),
                WorkerConfig(
                    concurrency=2,
                    lease_sec=0.6,
                    heartbeat_sec=0.1,
                    maintenance_sec=0.05,
                    poll_sec=0.02,
                ),
                ProcessExecutor(),
            )
            if maintenance:
                await worker.run_maintenance()
                return
            await worker.run()
        finally:
            await pool.close()


def run_worker(
    config: ProcessConfig,
    owner: str,
    worker_id: str,
    maintenance: bool = False,
    pause_after_claim: bool = False,
) -> None:
    KerberosWorkspace.configure(
        str(config.krb5_config), str(config.cache_dir / worker_id)
    )
    asyncio.run(
        ProcessWorker().run(
            config.postgres, owner, worker_id, maintenance, pause_after_claim
        )
    )
