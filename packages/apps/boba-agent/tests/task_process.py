"""Процесс независимого worker для интеграционных проверок."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspace
from boba.tasks.models import (
    ExecutionContext,
    TaskExecutor,
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


class ProcessExecutor(TaskExecutor):
    async def execute(self, task: TaskState, context: ExecutionContext) -> TaskOutcome:
        delay = task.payload["delay"]
        if not isinstance(delay, (int, float)):
            raise ValueError("Process test expects a numeric delay")
        await context.report_progress({"pid": os.getpid()})
        await asyncio.sleep(delay)
        return TaskOutcome(status=TaskStatus.COMPLETED, result={"pid": os.getpid()})


class ProcessWorker:
    async def run(
        self, postgres: PostgresConfig, owner: str, worker_id: str, maintenance: bool
    ) -> None:
        pool = AsyncPostgresPool(postgres)
        await pool.open()
        try:
            worker = TaskWorker(
                TaskService(pool),
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
    config: ProcessConfig, owner: str, worker_id: str, maintenance: bool = False
) -> None:
    KerberosWorkspace.configure(
        str(config.krb5_config), str(config.cache_dir / worker_id)
    )
    asyncio.run(ProcessWorker().run(config.postgres, owner, worker_id, maintenance))
