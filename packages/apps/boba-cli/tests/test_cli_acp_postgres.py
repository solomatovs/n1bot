"""Два процесса `boba acp` на одну сессию в Postgres стенда: держатель ведёт
ход, второй процесс возобновляет сессию с воспроизведением наблюдателем —
отдаёт историю теми же уведомлениями, его `prompt` отвергается, пока замок у
держателя, а после закрытия сессии держателем проходит; `session/list` видит сессию с
заголовком и каталогом; `session/delete` при занятом замке отвергается, без
замка удаляет записи, повтор — успех; `session/fork` сессии, которую держит
другой процесс, даёт наблюдателю свою сессию с той же беседой, и ход в ней
идёт при занятом замке исходной. Процесс входит в Postgres билетом
Kerberos из кэша тестового стенда: пути к кэшу и krb5.conf едут окружением,
секретов в окружении нет."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from acp.exceptions import RequestError
from acp.experimental.v2 import schema as acp_schema
from omegaconf import DictConfig
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_cli_acp import (
    AcpClient,
    AcpProcess,
    Document,
    FakeModel,
    FeedView,
    StandRegistration,
    call,
    handshake,
)

from boba.agent.history import SessionId, StoredLine
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import KeytabAuth
from boba.history.postgres import PostgresHistoryStore
from boba.krb import KerberosEnv, KerberosWorkspace
from boba.stand.database import TestDatabase
from boba.stand.ui.fake_llm import ScenarioName
from boba.transport.acp.protocol import (
    AcpErrorCode,
    MetaKey,
    StopReason,
)

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
            got = type(auth).__name__
            pytest.fail(
                f"the stand Postgres must be entered with a keytab: expected "
                f"KeytabAuth in [postgres].auth of the stand config, got {got}"
            )

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
    """Два процесса над одной сессией: держатель и наблюдатель, их клиенты,
    регистрации и соединения."""

    def __init__(self, stand: PostgresStand, model: FakeModel, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        self.holder = AcpProcess(tmp_path / "a", model, environment=stand.environment())
        self.observer = AcpProcess(
            tmp_path / "b", model, environment=stand.environment()
        )
        self.holder_client = AcpClient()
        self.observer_client = AcpClient()
        self.holder_registration = StandRegistration([StandRegistration.STAND])
        self.observer_registration = StandRegistration([StandRegistration.STAND])
        self._stand = stand
        self.holder_conn: Any = None
        self.observer_conn: Any = None

    async def start(self) -> None:
        self.holder_conn = await self.holder.start(
            self.holder_client,
            self.holder_registration,
            self._stand.settings(self.holder),
        )
        self.observer_conn = await self.observer.start(
            self.observer_client,
            self.observer_registration,
            self._stand.settings(self.observer),
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

    async def delete(self, session_id: str) -> None:
        await self.observer_conn.delete_session(session_id=session_id)


class TestTwoProcesses:
    PROMPT = call({"name": "mcp__stand__add", "arguments": {"a": 4, "b": 5}})[0].text

    async def test_observer_replays_and_takes_over_after_the_holder_leaves(
        self,
        stand: PostgresStand,
        pool: AsyncPostgresPool,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        pair = Pair(stand, model, tmp_path)
        await pair.start()
        try:
            session_id = await self.holder_turn(pair, tmp_path)
            await self.observer_replays_the_same_feed(pair, tmp_path, session_id)
            await self.observer_is_refused_while_held(pair, session_id)
            await self.listed_with_title(pair, session_id, tmp_path)

            await pair.holder_conn.close_session(session_id=session_id)
            await self.observer_takes_over(pair, session_id)

            await pair.observer_conn.close_session(session_id=session_id)
            store = stand.store(pool)
            human = await self.human_prompts(store, session_id)
            assert human == [self.PROMPT, ScenarioName.ANSWER.value]

            await pair.delete(session_id)
            assert await self.human_prompts(store, session_id) == []
            assert (await pair.observer_conn.list_sessions()).sessions == []
            await pair.delete(session_id)
        finally:
            await pair.stop()

    async def holder_turn(self, pair: Pair, cwd: Path) -> str:
        session = await pair.holder_conn.new_session(
            cwd=str(cwd), mcp_servers=[StandRegistration.STAND]
        )
        _, idle = await pair.holder.turn(
            pair.holder_client,
            session.session_id,
            [acp_schema.TextContentBlock(text=self.PROMPT)],
        )
        assert idle.stop_reason == StopReason.END_TURN

        return session.session_id

    async def observer_replays_the_same_feed(
        self, pair: Pair, cwd: Path, session_id: str
    ) -> None:
        view = FeedView()
        live = view.render(pair.holder_client.updates_of(session_id))
        await pair.observer_conn.resume_session(
            cwd=str(cwd),
            session_id=session_id,
            mcp_servers=[StandRegistration.STAND],
            replay_from=acp_schema.ReplayFromStartVariant(),
        )
        replayed = view.render(pair.observer_client.updates_of(session_id))

        assert view.user_texts(replayed) == [self.PROMPT]
        assert replayed == live
        assert len(view.without_user(replayed)) >= 3

    async def observer_is_refused_while_held(self, pair: Pair, session_id: str) -> None:
        with pytest.raises(RequestError) as held:
            await pair.observer_conn.prompt(
                session_id=session_id,
                prompt=[acp_schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )

        assert held.value.code == AcpErrorCode.INTERNAL_ERROR
        assert "held by another process" in str(held.value)

        with pytest.raises(RequestError) as undeletable:
            await pair.delete(session_id)

        assert undeletable.value.code == AcpErrorCode.INTERNAL_ERROR
        assert "held by another process" in str(undeletable.value)

    async def listed_with_title(self, pair: Pair, session_id: str, cwd: Path) -> None:
        listed = await pair.observer_conn.list_sessions()

        assert [s.session_id for s in listed.sessions] == [session_id]
        assert listed.sessions[0].cwd == str(cwd)
        assert listed.sessions[0].title == self.PROMPT
        assert listed.sessions[0].updated_at

    async def observer_takes_over(self, pair: Pair, session_id: str) -> None:
        _, taken = await pair.observer.turn(
            pair.observer_client,
            session_id,
            [acp_schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
        )
        assert taken.stop_reason == StopReason.END_TURN

        with pytest.raises(RequestError) as back:
            await pair.holder_conn.prompt(
                session_id=session_id, prompt=[acp_schema.TextContentBlock(text="x")]
            )

        assert back.value.code == AcpErrorCode.RESOURCE_NOT_FOUND

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


class TestForkOfAHeldSession:
    PROMPT = call({"name": "mcp__stand__add", "arguments": {"a": 1, "b": 1}})[0].text

    async def test_observer_forks_the_held_session_and_prompts_the_branch(
        self,
        stand: PostgresStand,
        pool: AsyncPostgresPool,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        pair = Pair(stand, model, tmp_path)
        replay = acp_schema.ReplayFromStartVariant()
        await pair.start()
        try:
            session = await pair.holder_conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            session_id = session.session_id
            for prompt in (self.PROMPT, ScenarioName.ANSWER.value):
                _, idle = await pair.holder.turn(
                    pair.holder_client,
                    session_id,
                    [acp_schema.TextContentBlock(text=prompt)],
                )
                assert idle.stop_reason == StopReason.END_TURN

            await pair.observer_conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=replay,
            )
            view = FeedView()
            original = view.render(pair.observer_client.updates_of(session_id))
            second_question = [item for item in original if item[0] == "user"][1]

            meta: dict[str, Any] = {MetaKey.UP_TO_MESSAGE_ID.value: second_question[1]}
            forked = await pair.observer_conn.fork_session(
                session_id=session_id,
                cwd=str(tmp_path),
                mcp_servers=[StandRegistration.STAND],
                **meta,
            )
            branch_id = forked.session_id
            await pair.observer_conn.resume_session(
                cwd=str(tmp_path),
                session_id=branch_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=replay,
            )
            branch = view.render(pair.observer_client.updates_of(branch_id))
            assert branch == original[: original.index(second_question)]

            _, taken = await pair.observer.turn(
                pair.observer_client,
                branch_id,
                [acp_schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert taken.stop_reason == StopReason.END_TURN

            with pytest.raises(RequestError) as held:
                await pair.observer_conn.prompt(
                    session_id=session_id,
                    prompt=[acp_schema.TextContentBlock(text="x")],
                )

            assert held.value.code == AcpErrorCode.INTERNAL_ERROR

            store = stand.store(pool)
            human = TestTwoProcesses.human_prompts
            assert await human(store, session_id) == [
                self.PROMPT,
                ScenarioName.ANSWER.value,
            ]
            assert await human(store, branch_id) == [
                self.PROMPT,
                ScenarioName.ANSWER.value,
            ]
            listed = await pair.holder_conn.list_sessions()
            assert sorted(s.session_id for s in listed.sessions) == sorted(
                [session_id, branch_id]
            )
            await pair.holder_conn.close_session(session_id=session_id)
            await pair.observer_conn.close_session(session_id=branch_id)
            await pair.observer_conn.close_session(session_id=session_id)
            await pair.delete(session_id)
            await pair.delete(branch_id)
        finally:
            await pair.stop()
