"""Ручная проверка TaskWorker на тестовой БД; использует исполнитель task_process.

Ошибки:
SystemExit — некорректный запуск, ошибка подключения или выполнения команды.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import tomllib
from enum import StrEnum
from functools import partial
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, NonNegativeFloat, PositiveFloat
from task_process import ProcessExecutor

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspace
from boba.tasks.models import TaskCreate, WorkerConfig, WorkerIdentity
from boba.tasks.service import TaskService
from boba.tasks.worker import TaskNotifications, TaskWorker


class DemoCommand(StrEnum):
    WORKER = "worker"
    ADD = "add"
    STATUS = "status"


class DemoOptions(BaseModel):
    command: DemoCommand
    config: Path
    database: str
    owner: str
    worker_id: str
    task_id: str | None
    delay: NonNegativeFloat
    timeout: PositiveFloat
    lease_sec: PositiveFloat


class TaskDemo:
    """Добавляет и читает задачи через TaskService либо запускает независимый worker."""

    def __init__(self, options: DemoOptions) -> None:
        self.options = options
        config_path = options.config.resolve()
        document = tomllib.loads(config_path.read_text())
        postgres = PostgresConfig.model_validate(document["postgres"])
        postgres = postgres.model_copy(update={"dbname": options.database})
        cache = Path(".dev/task-worker/demo-krb") / uuid4().hex
        KerberosWorkspace.configure(
            str(config_path.parent / "krb" / "krb5.conf"), str(cache.resolve())
        )
        self.pool = AsyncPostgresPool(postgres)
        self.service = TaskService(self.pool)
        self.notifications = TaskNotifications(
            partial(AsyncPostgresPool.dedicated, postgres)
        )
        self.worker = TaskWorker(
            self.service,
            WorkerIdentity(
                options.worker_id,
                frozenset({"process"}),
                owners=frozenset({options.owner}),
            ),
            WorkerConfig(
                concurrency=2, maintenance_sec=1, lease_sec=options.lease_sec
            ),
            ProcessExecutor(),
            self.notifications,
        )
        self.stopping = asyncio.Event()
        self.logger = logging.getLogger(__name__)

    async def run(self) -> None:
        await self.pool.open()
        try:
            await self._check_database()
            match self.options.command:
                case DemoCommand.WORKER:
                    await self._run_worker()

                case DemoCommand.ADD:
                    task_id = self.options.task_id
                    if task_id is None:
                        task_id = "task-demo-" + uuid4().hex

                    state = await self.service.add(
                        self.options.owner,
                        TaskCreate(
                            task_id=task_id,
                            kind="process",
                            payload={"delay": self.options.delay},
                            run_timeout_sec=self.options.timeout,
                        ),
                    )
                    print(state.model_dump_json(indent=2), flush=True)

                case DemoCommand.STATUS:
                    if self.options.task_id is None:
                        raise ValueError("The status command requires --task-id")

                    state = await self.service.status(
                        self.options.owner, self.options.task_id
                    )
                    print(state.model_dump_json(indent=2), flush=True)

        finally:
            await self.notifications.close()
            await self.pool.close()

    async def _check_database(self) -> None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "select shobj_description(oid, 'pg_database') "
                "from pg_database where datname = current_database()"
            )
            row = await cursor.fetchone()

        if row != ("boba history-sql experiment v1",):
            raise ValueError(
                f"Database {self.options.database}: expected the boba "
                f"history-sql experiment marker, got {row!r}"
            )

    async def _run_worker(self) -> None:
        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, self.stopping.set)

        try:
            await self.notifications.start()
            async with asyncio.TaskGroup() as group:
                group.create_task(self.worker.run(), name=self.options.worker_id)
                await self.worker.started.wait()
                self.logger.info(
                    "Worker %s: ready; database=%s owner=%s kind=process",
                    self.options.worker_id,
                    self.options.database,
                    self.options.owner,
                )
                group.create_task(self._stop_worker(), name="demo-stop")

        finally:
            for name in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(name)

    async def _stop_worker(self) -> None:
        await self.stopping.wait()
        await self.worker.stop()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", type=DemoCommand, choices=tuple(DemoCommand))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--owner", default="task-demo")
    parser.add_argument("--worker-id", default="task-demo-worker")
    parser.add_argument("--task-id")
    parser.add_argument("--delay", type=float, default=15)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--lease-sec", type=float, default=60)
    try:
        options = DemoOptions.model_validate(vars(parser.parse_args()))
        await TaskDemo(options).run()

    except Exception as exc:
        raise SystemExit(f"Task demo failed: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
