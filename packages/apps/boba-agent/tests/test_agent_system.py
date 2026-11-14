"""Проверки исполнения агента без протокола клиента."""

import psycopg
import pytest
from agent_stand import AgentSystem

from boba.agent import models as m
from boba.agent.acp_server import SessionHub
from boba.agent.history import HistoryStore
from boba.db.postgres import AsyncPostgresPool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_failed_session_creation_releases_execution_ownership(
    system: AgentSystem,
) -> None:
    async def read_only(connection: psycopg.AsyncConnection) -> None:
        await connection.execute("set default_transaction_read_only = on")

    pool = AsyncPostgresPool(system.postgres, configure=read_only)
    await pool.open()
    history = HistoryStore(pool)
    hub = SessionHub(history, system.llm, system.profiles, system.settings, "test")
    await hub.start(system.postgres)
    try:
        assert hub.owner_connection is not None
        pid = hub.owner_connection.info.backend_pid
        with pytest.raises(m.HistoryWriteError) as failure:
            await hub.create(system.owner, frozenset({"test"}), "/workspace")

        assert isinstance(
            failure.value.__cause__, psycopg.errors.ReadOnlySqlTransaction
        )
        assert not hub.agents
        async with system.history.pool.connection() as connection:
            cursor = await connection.execute(
                "select count(*) from pg_locks "
                "where pid = %s and locktype = 'advisory'",
                (pid,),
            )
            assert await cursor.fetchone() == (0,)

    finally:
        await hub.close()
        await pool.close()
