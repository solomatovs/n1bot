"""Реальная таблица tasks: владение, идемпотентность и независимые исполнители."""

import asyncio
from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest

from boba.db.postgres import AsyncPostgresPool
from boba.tasks.models import (
    ExecutionContext,
    TaskCode,
    TaskCreate,
    TaskExecutor,
    TaskOutcome,
    TaskServiceError,
    TaskState,
    TaskStatus,
    WorkerConfig,
    WorkerIdentity,
)
from boba.tasks.service import TaskService
from boba.tasks.worker import TaskNotifications, TaskWorker

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
async def tasks(history):
    service = TaskService(history.pool)
    owner = "tasks-test-" + uuid4().hex
    try:
        yield service, owner
    finally:
        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.tasks where owner_id = %s", (owner,)
            )


@pytest.fixture
def process_config(postgres, tmp_path, pytestconfig):
    from task_process import ProcessConfig

    path = Path(pytestconfig.getoption("--agent-config")).resolve()
    return ProcessConfig(postgres, path.parent / "krb" / "krb5.conf", tmp_path)


def definition(**changes):
    values = dict(
        task_id=uuid4().hex, kind="echo", payload={"text": "hello"}, run_timeout_sec=10
    )
    values.update(changes)
    return TaskCreate.model_validate(values)


def identity(owner: str, *, routes: frozenset[str] = frozenset()) -> WorkerIdentity:
    return WorkerIdentity(uuid4().hex, frozenset({"echo"}), routes, frozenset({owner}))


async def test_add_is_idempotent_and_status_is_read_only(tasks):
    service, owner = tasks
    command = definition()
    first = await service.add(owner, command)
    assert first.status is TaskStatus.PENDING
    assert first.revision == 1
    assert await service.add(owner, command) == first
    assert await service.status(owner, command.task_id) == first
    with pytest.raises(TaskServiceError) as conflict:
        await service.add(
            owner, command.model_copy(update={"payload": {"text": "different"}})
        )
    assert conflict.value.code is TaskCode.CONFLICT
    with pytest.raises(TaskServiceError) as forbidden:
        await service.status("another-user", command.task_id)
    assert forbidden.value.code is TaskCode.NOT_FOUND
    with pytest.raises(TaskServiceError):
        await service.add("another-user", command)


async def test_only_one_claim_and_finish_rejects_foreign_token(tasks):
    service, owner = tasks
    command = definition()
    await service.add(owner, command)
    workers = [identity(owner) for _ in range(16)]
    claimed = await asyncio.gather(
        *(service.claim(worker, uuid4(), 5) for worker in workers)
    )
    leases = [lease for lease in claimed if lease is not None]
    assert len(leases) == 1
    lease = leases[0]
    assert lease.state.status is TaskStatus.RUNNING
    outcome = TaskOutcome(status=TaskStatus.COMPLETED, result={"text": "done"})
    with pytest.raises(TaskServiceError) as lost:
        await service.finish(replace(lease, token=uuid4()), outcome)
    assert lost.value.code is TaskCode.LEASE_LOST
    renewed = await service.renew(lease, 5)
    assert renewed.state.revision == lease.state.revision
    result = await service.finish(lease, outcome)
    assert result.status is TaskStatus.COMPLETED
    assert await service.finish(lease, outcome) == result
    assert await service.claim(workers[0], uuid4(), 5) is None


async def test_permissions_routes_and_claim_recovery(tasks):
    service, owner = tasks
    command = definition(route_key="client-channel")
    await service.add(owner, command)
    assert await service.claim(identity(owner), uuid4(), 5) is None
    permitted = identity(owner, routes=frozenset({"client-channel"}))
    token = uuid4()
    lease = await service.claim(permitted, token, 5)
    assert lease is not None
    assert await service.claim(permitted, token, 5) == lease
    assert await service.recover(identity(owner), token) is None


async def test_expiry_blocks_stale_result_before_and_after_maintenance(tasks):
    service, owner = tasks
    command = definition()
    await service.add(owner, command)
    lease = await service.claim(identity(owner), uuid4(), 0.1)
    assert lease is not None
    await asyncio.sleep(0.15)
    outcome = TaskOutcome(status=TaskStatus.COMPLETED, result="late")
    with pytest.raises(TaskServiceError):
        await service.finish(lease, outcome)
    with pytest.raises(TaskServiceError):
        await service.renew(lease, 5)
    await asyncio.gather(service.expire(), service.expire())
    state = await service.status(owner, command.task_id)
    assert state.status is TaskStatus.INTERRUPTED
    assert state.revision == 3
    assert state.error is not None
    assert state.error.code == "lease_expired"
    with pytest.raises(TaskServiceError):
        await service.finish(lease, outcome)
    assert await service.claim(identity(owner), uuid4(), 5) is None


class EchoExecutor(TaskExecutor):
    async def execute(self, task: TaskState, context: ExecutionContext) -> TaskOutcome:
        await context.report_progress({"step": "started"})
        await asyncio.sleep(0.03)
        return TaskOutcome(status=TaskStatus.COMPLETED, result=task.payload)


class HangingExecutor(TaskExecutor):
    def __init__(self):
        self.cancelled = asyncio.Event()

    async def execute(self, task: TaskState, context: ExecutionContext) -> TaskOutcome:
        try:
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")
        finally:
            self.cancelled.set()


async def test_workers_execute_without_agent_or_history(tasks, agent_timeout):
    service, owner = tasks
    config = WorkerConfig(
        lease_sec=2, heartbeat_sec=0.1, poll_sec=0.01, maintenance_sec=0.05
    )
    commands = [definition() for _ in range(24)]
    for command in commands:
        await service.add(owner, command)
    workers = [
        TaskWorker(service, identity(owner), config, EchoExecutor()) for _ in range(3)
    ]
    processes = [asyncio.create_task(worker.run()) for worker in workers]
    try:
        async with asyncio.timeout(agent_timeout):
            while True:
                states = await asyncio.gather(
                    *(service.status(owner, item.task_id) for item in commands)
                )
                if all(state.status.terminal for state in states):
                    break
                await asyncio.sleep(0.02)
        assert all(state.status is TaskStatus.COMPLETED for state in states)
        assert all(state.revision == 4 for state in states)
        assert all(state.result == {"text": "hello"} for state in states)
    finally:
        await asyncio.gather(*(worker.stop() for worker in workers))
        await asyncio.gather(*processes)


async def test_execution_deadline_survives_healthy_heartbeats(tasks, agent_timeout):
    service, owner = tasks
    command = definition(run_timeout_sec=0.25)
    await service.add(owner, command)
    executor = HangingExecutor()
    config = WorkerConfig(
        lease_sec=1, heartbeat_sec=0.05, poll_sec=0.02, maintenance_sec=0.03
    )
    worker = TaskWorker(service, identity(owner), config, executor)
    process = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(agent_timeout):
            await executor.cancelled.wait()
            while True:
                state = await service.status(owner, command.task_id)
                if state.status.terminal:
                    break
                await asyncio.sleep(0.02)
        assert state.status is TaskStatus.INTERRUPTED
        assert state.error is not None
        assert state.error.code == "execution_timeout"
    finally:
        await worker.stop()
        await process


async def test_notifications_follow_commit_and_reconnect(
    tasks, postgres, agent_timeout
):
    service, owner = tasks
    listener = TaskNotifications(partial(AsyncPostgresPool.dedicated, postgres), 0.02)
    event = asyncio.Event()
    seen = []
    command = definition()

    def changed(value):
        seen.append(value)
        event.set()

    listener.subscribe(command.task_id, changed)
    try:
        async with asyncio.timeout(agent_timeout):
            await listener.start()
            event.clear()
            seen.clear()
            async with service.pool.connection() as connection:
                async with connection.transaction(force_rollback=True):
                    await service.add(owner, command, connection=connection)
            await asyncio.sleep(0.1)
            assert not seen
            await service.add(owner, command)
            await event.wait()
            assert seen[-1].revision == 1
            event.clear()
            old_pid = listener.backend_pid
            async with service.pool.connection() as connection:
                await connection.execute("select pg_terminate_backend(%s)", (old_pid,))
            await event.wait()
            assert seen[-1] is None
            assert listener.backend_pid != old_pid
            assert (
                await service.status(owner, command.task_id)
            ).status is TaskStatus.PENDING
    finally:
        await listener.close()


async def test_separate_process_recovers_task_after_worker_is_killed(
    tasks, process_config, agent_timeout
):
    import multiprocessing

    from task_process import run_worker

    service, owner = tasks
    command = definition(kind="process", payload={"delay": 100})
    await service.add(owner, command)
    context = multiprocessing.get_context("spawn")
    worker = context.Process(
        target=run_worker, args=(process_config, owner, uuid4().hex)
    )
    maintenance = context.Process(
        target=run_worker, args=(process_config, owner, uuid4().hex, True)
    )
    worker.start()
    try:
        async with asyncio.timeout(agent_timeout):
            while True:
                state = await service.status(owner, command.task_id)
                if state.progress is not None:
                    break
                await asyncio.sleep(0.02)
            worker.kill()
            await asyncio.to_thread(worker.join, 2)
            maintenance.start()
            while True:
                state = await service.status(owner, command.task_id)
                if state.status.terminal:
                    break
                await asyncio.sleep(0.02)
        assert state.status is TaskStatus.INTERRUPTED
        assert state.error is not None
        assert state.error.code == "lease_expired"
        assert state.result is None
    finally:
        for process in (worker, maintenance):
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 2)
            if process.pid is not None:
                process.close()


async def test_multiple_worker_processes_share_one_queue(
    tasks, process_config, agent_timeout
):
    import multiprocessing

    from task_process import run_worker

    service, owner = tasks
    commands = [definition(kind="process", payload={"delay": 0.15}) for _ in range(24)]
    for command in commands:
        await service.add(owner, command)
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(target=run_worker, args=(process_config, owner, uuid4().hex))
        for _ in range(3)
    ]
    for process in workers:
        process.start()
    try:
        async with asyncio.timeout(agent_timeout):
            while True:
                states = await asyncio.gather(
                    *(service.status(owner, item.task_id) for item in commands)
                )
                if all(state.status.terminal for state in states):
                    break
                await asyncio.sleep(0.05)
        assert all(state.status is TaskStatus.COMPLETED for state in states)
        pids = set()
        for state in states:
            assert state.revision == 4
            assert isinstance(state.result, dict)
            pids.add(state.result["pid"])
        assert len(pids) >= 2
    finally:
        for process in workers:
            if process.is_alive():
                process.kill()
            await asyncio.to_thread(process.join, 2)
            process.close()


async def test_claim_skips_locked_task_and_does_not_block_readers(tasks):
    service, owner = tasks
    first, second = definition(), definition()
    await service.add(owner, first)
    await service.add(owner, second)
    async with service.pool.connection() as connection, connection.transaction():
        await connection.execute(
            "select task_id from boba.tasks where task_id = %s for update",
            (first.task_id,),
        )
        async with asyncio.timeout(2):
            lease = await service.claim(identity(owner), uuid4(), 5)
            assert lease is not None
            assert lease.state.task_id == second.task_id
            assert (
                await service.status(owner, first.task_id)
            ).status is TaskStatus.PENDING
    available = await service.claim(identity(owner), uuid4(), 5)
    assert available is not None
    assert available.state.task_id == first.task_id


async def test_progress_and_failed_outcome_are_persistent(tasks):
    from boba.tasks.models import TaskError

    service, owner = tasks
    command = definition()
    await service.add(owner, command)
    lease = await service.claim(identity(owner), uuid4(), 5)
    assert lease is not None
    observed = await service.progress(lease, {"percent": 50})
    assert observed.revision == 3
    assert observed.progress == {"percent": 50}
    failed = await service.finish(
        lease,
        TaskOutcome(
            status=TaskStatus.FAILED,
            error=TaskError(code="remote_error", message="Remote operation failed"),
        ),
    )
    assert failed.revision == 4
    assert failed.error is not None
    assert failed.error.code == "remote_error"
    with pytest.raises(TaskServiceError):
        await service.progress(lease, {"percent": 100})
    assert await service.status(owner, command.task_id) == failed
