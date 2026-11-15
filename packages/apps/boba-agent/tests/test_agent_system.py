"""Проверки исполнения агента без протокола клиента."""

import psycopg
import pytest
from agent_stand import AgentSystem

from boba.agent import models as m
from boba.agent.acp_server import SessionHub
from boba.agent.history import HistoryStore
from boba.db.postgres import AsyncPostgresPool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_personal_provider_does_not_grant_access_to_model_profile(
    system: AgentSystem,
) -> None:
    agent = await system.hub.create(system.owner, frozenset({"hidden"}), "/workspace")
    selected = await system.history.latest(agent.session_id, kind="model")
    model = m.ModelRecord.model_validate(selected[0]).payload
    assert model.provider == "test"
    assert model.profile_id == "hidden"
    with pytest.raises(PermissionError, match="hidden"):
        await system.hub.get(agent.session_id, system.owner, frozenset({"test"}))


async def test_failed_session_creation_releases_execution_ownership(
    system: AgentSystem,
) -> None:
    async def read_only(connection: psycopg.AsyncConnection) -> None:
        await connection.execute("set default_transaction_read_only = on")

    pool = AsyncPostgresPool(system.postgres, configure=read_only)
    await pool.open()
    history = HistoryStore(pool)
    hub = SessionHub(
        history,
        system.llm,
        system.profiles,
        system.settings,
        "test",
        providers=system.providers,
    )
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


async def test_resume_restores_profile_identity_from_saved_choice(
    system: AgentSystem,
) -> None:
    agent = await system.hub.create(system.owner, frozenset({"test"}), "/workspace")
    payload = {"provider": "test", "value": {"model": "system-test-model"}}
    await agent.append(
        [
            agent.make(m.ModelRecord, payload=payload),
            agent.make(
                m.ParameterOptionRecord,
                key="model",
                payload={
                    "name": "Model",
                    "source_kinds": ["model"],
                    "source_key": "model",
                    "value_path": [],
                    "choices": [
                        {"value_id": "test", "name": "Test preset", "value": payload}
                    ],
                },
            ),
        ]
    )
    sid = agent.session_id
    await system.restart()
    await system.hub.get(sid, system.owner, frozenset({"test"}))
    model = await system.history.latest(sid, kind="model")
    selected = m.ModelRecord.model_validate(model[0]).payload
    assert selected.profile_id == "test"
    assert selected.provider == "test"
    assert selected.value == payload["value"]
    options = await system.history.latest(sid, kind="parameter_option", key="model")
    definition = m.ParameterOptionRecord.model_validate(options[0]).payload
    assert definition.value_path == ("profile_id",)
    assert definition.choose("test").value == "test"
