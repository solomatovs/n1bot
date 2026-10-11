"""Два процесса `boba acp` на одну сессию в Postgres стенда: держатель ведёт
ход, второй процесс загружает сессию наблюдателем — отдаёт историю теми же
уведомлениями, его `prompt` отвергается, пока замок у держателя, а после
закрытия сессии держателем проходит; `session/list` видит сессию с
заголовком и каталогом; `session/delete` при занятом замке отвергается, без
замка удаляет записи, повтор — успех. Процесс входит в Postgres билетом
Kerberos из кэша тестового стенда: пути к кэшу и krb5.conf едут окружением,
секретов в окружении нет."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from acp import text_block
from acp.exceptions import RequestError
from omegaconf import DictConfig
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_cli_acp import (
    INTERNAL_ERROR,
    RESOURCE_NOT_FOUND,
    AcpProcess,
    Document,
    FakeModel,
    FeedView,
    McpStand,
    RecordingClient,
    call,
    handshake,
)

from boba.agent.history import SessionId, StoredLine
from boba.cli.acp import StopReasonName
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import KeytabAuth
from boba.history.postgres import PostgresHistoryStore
from boba.krb import KerberosEnv, KerberosWorkspace
from boba.stand.database import TestDatabase
from boba.stand.ui.fake_llm import ScenarioName

pytestmark = pytest.mark.anyio


@pytest.fixture
async def postgres(raw_config: DictConfig) -> PostgresConfig:
    cfg = bind(raw_config, path="postgres", model=PostgresConfig)
    database = await TestDatabase.ensure(cfg)

    return TestDatabase.config_of(cfg, database)


@pytest.fixture
async def pool(postgres: PostgresConfig) -> AsyncIterator[AsyncPostgresPool]:
    opened = AsyncPostgresPool(postgres)
    await opened.open()
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
async def schema(pool: AsyncPostgresPool) -> AsyncIterator[str]:
    """Своя схема истории на тест; после теста сносится."""
    name = f"acp_hist_{uuid.uuid4().hex[:12]}"
    yield name
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name))
        )


class PostgresStand:
    """Настройки процесса для истории в Postgres стенда: строка подключения
    без пароля (вход билетом Kerberos) и окружение libkrb5 с кэшем билета,
    который стенд уже получил по keytab."""

    def __init__(self, postgres: PostgresConfig, schema: str) -> None:
        self._postgres = postgres
        self._schema = schema

    def environment(self) -> dict[str, str]:
        auth = self._postgres.auth
        if not isinstance(auth, KeytabAuth):
            pytest.skip("the stand Postgres is not entered with a keytab")

        return {
            KerberosEnv.CCACHE: KerberosWorkspace.ccache_for(auth),
            KerberosEnv.CONFIG: KerberosWorkspace.krb5_config(),
        }

    def dsn(self) -> str:
        auth = self._postgres.auth
        assert isinstance(auth, KeytabAuth)

        return make_conninfo(
            host=self._postgres.host,
            hostaddr=self._postgres.hostaddr,
            port=self._postgres.port,
            dbname=self._postgres.dbname,
            user=auth.principal.partition("@")[0],
            sslmode=self._postgres.sslmode,
        )

    def settings(self, process: AcpProcess) -> Document:
        document = process.settings_document(
            mode="bypassPermissions", allow_bypass=True
        )
        document["history"] = {
            "kind": "postgres",
            "dsn": self.dsn(),
            "schema": self._schema,
        }

        return document

    def store(self, pool: AsyncPostgresPool) -> PostgresHistoryStore:
        return PostgresHistoryStore(self._postgres, self._schema, pool)


@pytest.fixture
def stand(postgres: PostgresConfig, schema: str) -> PostgresStand:
    return PostgresStand(postgres, schema)


class Pair:
    """Два процесса над одной сессией: держатель и наблюдатель, их клиенты
    и соединения."""

    def __init__(self, stand: PostgresStand, model: FakeModel, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        self.holder = AcpProcess(tmp_path / "a", model, environment=stand.environment())
        self.observer = AcpProcess(
            tmp_path / "b", model, environment=stand.environment()
        )
        self.holder_client = RecordingClient()
        self.observer_client = RecordingClient()
        self._stand = stand
        self.holder_conn: Any = None
        self.observer_conn: Any = None

    async def start(self) -> None:
        self.holder_conn = await self.holder.start(
            self.holder_client, self._stand.settings(self.holder)
        )
        self.observer_conn = await self.observer.start(
            self.observer_client, self._stand.settings(self.observer)
        )
        await handshake(self.holder_conn)
        await handshake(self.observer_conn)

    async def stop(self) -> None:
        await self.holder_conn.close()
        await self.observer_conn.close()
        await self.holder.close_stdin()
        await self.observer.close_stdin()
        assert await self.holder.wait() == 0
        assert await self.observer.wait() == 0

    async def delete(self, session_id: str) -> Any:
        return await self.observer_conn._conn.send_request(
            "session/delete", {"sessionId": session_id}
        )


class TestTwoProcesses:
    PROMPT = call({"name": "add", "arguments": {"a": 4, "b": 5}})

    async def test_observer_replays_and_takes_over_after_the_holder_leaves(
        self,
        stand: PostgresStand,
        pool: AsyncPostgresPool,
        model: FakeModel,
        mcp: McpStand,
        tmp_path: Path,
    ) -> None:
        pair = Pair(stand, model, tmp_path)
        await pair.start()
        try:
            session_id = await self.holder_turn(pair, mcp, tmp_path)
            await self.observer_loads_the_same_feed(pair, mcp, tmp_path, session_id)
            await self.observer_is_refused_while_held(pair, session_id)
            await self.listed_with_title(pair, session_id, tmp_path)

            await pair.holder_conn.close_session(session_id=session_id)
            await self.observer_takes_over(pair, session_id)

            await pair.observer_conn.close_session(session_id=session_id)
            store = stand.store(pool)
            human = await self.human_prompts(store, session_id)
            assert human == [self.PROMPT, ScenarioName.ANSWER.value]

            assert await pair.delete(session_id) == {}
            assert await self.human_prompts(store, session_id) == []
            assert (await pair.observer_conn.list_sessions()).sessions == []
            assert await pair.delete(session_id) == {}
        finally:
            await pair.stop()

    async def holder_turn(self, pair: Pair, mcp: McpStand, cwd: Path) -> str:
        session = await pair.holder_conn.new_session(
            cwd=str(cwd), mcp_servers=[mcp.server()]
        )
        response = await pair.holder_conn.prompt(
            session_id=session.session_id, prompt=[text_block(self.PROMPT)]
        )
        assert response.stop_reason == StopReasonName.END_TURN.value

        return session.session_id

    async def observer_loads_the_same_feed(
        self, pair: Pair, mcp: McpStand, cwd: Path, session_id: str
    ) -> None:
        view = FeedView()
        live = view.render(pair.holder_client.updates_of(session_id))
        await pair.observer_conn.load_session(
            cwd=str(cwd), session_id=session_id, mcp_servers=[mcp.server()]
        )
        replayed = view.render(pair.observer_client.updates_of(session_id))

        assert view.user_texts(replayed) == [self.PROMPT]
        assert view.without_user(replayed) == view.without_user(live)
        assert len(view.without_user(replayed)) >= 3

    async def observer_is_refused_while_held(self, pair: Pair, session_id: str) -> None:
        with pytest.raises(RequestError) as held:
            await pair.observer_conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )

        assert held.value.code == INTERNAL_ERROR
        assert "held by another process" in str(held.value)

        with pytest.raises(RequestError) as undeletable:
            await pair.delete(session_id)

        assert undeletable.value.code == INTERNAL_ERROR
        assert "held by another process" in str(undeletable.value)

    async def listed_with_title(self, pair: Pair, session_id: str, cwd: Path) -> None:
        listed = await pair.observer_conn.list_sessions()

        assert [s.session_id for s in listed.sessions] == [session_id]
        assert listed.sessions[0].cwd == str(cwd)
        assert listed.sessions[0].title == self.PROMPT
        assert listed.sessions[0].updated_at

    async def observer_takes_over(self, pair: Pair, session_id: str) -> None:
        taken = await pair.observer_conn.prompt(
            session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
        )
        assert taken.stop_reason == StopReasonName.END_TURN.value

        with pytest.raises(RequestError) as back:
            await pair.holder_conn.prompt(
                session_id=session_id, prompt=[text_block("x")]
            )

        assert back.value.code == RESOURCE_NOT_FOUND

    @staticmethod
    async def human_prompts(store: PostgresHistoryStore, session_id: str) -> list[str]:
        found: list[str] = []
        async for line in store.read(SessionId(value=session_id)):
            assert isinstance(line, StoredLine)
            raw = line.raw
            if raw.get("type") != "user":
                continue

            origin = raw.get("origin")
            if not isinstance(origin, dict) or origin.get("kind") != "human":
                continue

            found.append(str(raw["message"]["content"]))

        return found
