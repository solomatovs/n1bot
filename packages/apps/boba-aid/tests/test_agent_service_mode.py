"""Агент чата в режиме службы: [agent.process] называет адрес службы агента
(`boba serve` на стенде в цикле теста) и вход proxy; вкладка входит в
службу от имени своего пользователя, профиль агента выбирается настройкой
сессии `model` по имени профиля чата, ход идёт сессией службы и каждая
запись истории приходит `session_info_update` с `updatedAt`, закрытие
вкладки закрывает её; две вкладки одного пользователя — два подключения,
ходы которых идут поочерёдно. Удаление треда стирает сессию через службу от
имени владельца треда (логин и роли — из его строки users) и не зависит от
профилей чата в конфиге; профиль, которого служба не предлагает, — отказ с
понятной причиной."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest
from acp.experimental.v2 import schema
from acp_stand import AgentHistoryStand, AgentStand, FakeModel, RecordingUpdates
from aid_stand import NoConnectionStore, RelayStand, TabSession
from pydantic import SecretStr

from boba.aid.agent.chat import (
    AgentLauncher,
    AgentSessionMeta,
    Question,
    ThreadAgent,
    ThreadAgentsIndex,
    ThreadDeleter,
    ThreadIdentity,
)
from boba.aid.agent.client import AgentFailureKind, AgentProcessError
from boba.aid.agent.config import AgentConfig, AgentServiceConfig
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chat.profiles import ChatProfileConfig, ChatProfiles, SelectedProfile
from boba.chat.threads import StoredThread, ThreadUpsert
from boba.cli.serve import ServeHistory
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import CallContexts
from boba.identity.session import Login
from boba.identity.signin import SignInMetadata
from boba.mcp_client.client import (
    HttpLocation,
    HttpRoute,
    McpServers,
    McpServersConfig,
    ProxyAuth,
)
from boba.runtime.config import ProcessConfig
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.stand.agent_service import (
    AgentServiceStand,
    ServeAddress,
    ServeStandConfig,
    ServiceProfiles,
)
from boba.stand.names import StandNames
from boba.stand.signin import SignInStand
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import Reply, ReplyBookClient, ReplyTurn, ScenarioName
from boba.stand.ui.stand import StandPorts, StandUrl
from boba.transport.http import HttpTransportConfig

pytestmark = pytest.mark.anyio

SCHEMA = StandNames().of_tree("agent_service_mode")
NO_SERVERS = McpServersConfig(servers={})
MODEL = "fake-model"
FOREIGN_PROFILE = "absent"


@pytest.fixture(autouse=True)
def chainlit_context(auth_token: str) -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков.
    Секрет токенов chainlit ставит auth_token: вход вкладки подписывает им
    токен, и без него тест зависел бы от порядка прогона."""


@pytest.fixture
async def runtime_schema(pool: AsyncPostgresPool) -> None:
    """Таблицы чата в схеме набора: threads и users."""
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_files(
            conn, files("boba.runtime") / "schema", {}
        )


@pytest.fixture
async def threads(test_postgres: PostgresConfig, runtime_schema: None) -> ThreadsTable:
    """Таблица тредов: в threads.meta агент вкладки помнит сессию и профиль."""
    return ThreadsTable(test_postgres, SCHEMA)


@pytest.fixture
async def users(
    test_postgres: PostgresConfig, pool: AsyncPostgresPool, runtime_schema: None
) -> UsersTable:
    """Строки users: по ним удаление треда входит в службу от имени владельца."""
    return UsersTable(test_postgres, SCHEMA, pool)


@pytest.fixture
async def service(
    stand: Stand, model: FakeModel, process_config: ProcessConfig, tmp_path: Path
) -> AsyncIterator[AgentServiceStand]:
    """Служба агента с историей в Postgres стенда (роль с паролем) и
    профилем агента, названным как профиль чата стенда."""
    history = AgentHistoryStand(stand)
    layer: dict[str, Any] = {
        "permissions": {"defaultMode": "bypassPermissions", "allowBypass": True},
        "sessions": {"maxOpen": 4},
        "limits": {"toolResultWindowShare": 0.25},
    }
    profiles = ServiceProfiles(model.base_url()).add(
        SignInStand.PROFILE, ["dev"], MODEL
    )
    port = StandPorts().free()
    served = AgentServiceStand(
        process_config.krb,
        ServeHistory(schema=history.schema(), postgres=history.postgres()),
        layer,
        profiles,
        600.0,
        ServeAddress(listen=port, public=port),
    )
    await served.start()
    try:
        yield served
    finally:
        await served.stop()


class ServiceModeStand:
    """Запуск агента чата в режиме службы, вкладка пользователя alice с
    ролью dev и удаление её треда."""

    LOGIN = "alice"

    def __init__(  # noqa: PLR0913 — стенд собирается фикстурами набора
        self,
        stand: Stand,
        service: AgentServiceStand,
        model: FakeModel,
        users: UsersTable,
        threads: ThreadsTable,
        tmp_path: Path,
    ) -> None:
        history = AgentHistoryStand(stand)
        self.history = history
        location = HttpLocation(
            scheme="http",
            host="127.0.0.1",
            port=service.port,
            path=ServeStandConfig.PREFIX,
        )
        mode = AgentServiceConfig(
            endpoint=HttpRoute(service=location, path=ServeStandConfig.PATH),
            auth=ProxyAuth(
                issuer=location,
                client_id=ServeStandConfig.CLIENT_ID,
                client_secret=SecretStr(ServeStandConfig.CLIENT_SECRET),
                secret=SecretStr(ServeStandConfig.PROXY_SECRET),
            ),
            start_timeout_sec=60.0,
            request_timeout_sec=60.0,
            silence_timeout_sec=60.0,
        )
        base = AgentStand(tmp_path / "agents", history.history()).config()
        self.config: AgentConfig = base.model_copy(update={"process": mode})
        credentials = KerberosCredentialSource(None, NoRefresh())
        self._relays = RelayStand(
            NoConnectionStore(), lambda: credentials, CallContexts()
        )
        self._servers = McpServers(NO_SERVERS)
        self._users = users
        self._threads = threads
        self._model = model
        self.launcher = self.launcher_of(
            ChatProfiles({SignInStand.PROFILE: self.profile()}), self.config
        )
        self.menus = self._relays.menus(self._servers)

    def launcher_of(self, profiles: ChatProfiles, config: AgentConfig) -> AgentLauncher:
        """Запуск агента конфигом config при профилях чата profiles."""
        return AgentLauncher(
            config,
            NO_SERVERS,
            profiles,
            self._users,
            self._relays.registrations,
            self._relays.relay(self._servers),
            "0.0",
        )

    async def identity(self, profile: str) -> ThreadIdentity:
        """Вкладка alice в новом треде: строка users с ролями входа и строка
        треда с владельцем, как их оставляет вход и слой данных."""
        sign_in = SignInMetadata(roles=frozenset({"dev"}))
        owner = await self._users.upsert(Login(self.LOGIN), sign_in.render())
        thread_id = str(uuid4())
        await self._threads.upsert(ThreadUpsert(id=UUID(thread_id), user_id=owner.id))
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))

        return ThreadIdentity(
            thread_id=thread_id,
            user_id=owner.id,
            login=self.LOGIN,
            roles=frozenset({"dev"}),
            session=tab.live,
            profile=SelectedProfile(name=profile, config=self.profile()),
        )

    def profile(self) -> ChatProfileConfig:
        provider = {
            "kind": "openai",
            "connection": {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": self._model.port,
                "path": "/v1",
            },
            "transport": HttpTransportConfig().model_dump(mode="json"),
        }
        base = SignInStand.profile().model_dump(mode="json")

        return ChatProfileConfig.model_validate(
            {**base, "provider": provider, "model": MODEL}
        )

    async def opened(self, updates: RecordingUpdates) -> tuple[ThreadIdentity, str]:
        """Тред с одним ходом через службу: вкладка и id сессии агента;
        обновления хода копятся в updates."""
        identity = await self.identity(SignInStand.PROFILE)
        agent = ThreadAgent(
            identity,
            self.launcher,
            AgentSessionMeta(self._threads),
            updates,
            self.menus,
        )
        try:
            await agent.start()
            await agent.ask(Question(key="first", text=ScenarioName.ANSWER.value))
            session_id = agent.session_id
        finally:
            await agent.close()

        assert updates.finished, updates.updates
        assert session_id is not None

        return identity, session_id

    def answering_within(self, request_timeout_sec: float) -> AgentConfig:
        """Конфиг чата, в котором служба отвечает на метод сессии в срок
        request_timeout_sec."""
        process = self.config.process.model_copy(
            update={"request_timeout_sec": request_timeout_sec}
        )

        return self.config.model_copy(update={"process": process})

    async def stored(self, thread_id: str) -> StoredThread:
        stored = await self._threads.get(UUID(thread_id))
        assert stored is not None

        return stored

    async def stop(self) -> None:
        await self._servers.stop()


@pytest.fixture
async def mode(  # noqa: PLR0913 — стенд собирается фикстурами набора
    stand: Stand,
    service: AgentServiceStand,
    model: FakeModel,
    users: UsersTable,
    threads: ThreadsTable,
    tmp_path: Path,
) -> AsyncIterator[ServiceModeStand]:
    made = ServiceModeStand(stand, service, model, users, threads, tmp_path)
    try:
        yield made
    finally:
        await made.stop()


class TestServiceMode:
    async def test_turn_goes_through_the_agent_service(
        self, mode: ServiceModeStand, service: AgentServiceStand
    ) -> None:
        updates = RecordingUpdates()
        identity, session_id = await mode.opened(updates)

        # служба, как и `boba --stdio`, сообщает о каждой записи истории
        activity: list[datetime] = []
        for _, update in updates.updates:
            if not isinstance(update, schema.SessionInfoUpdate):
                continue

            if update.updated_at is not None:
                activity.append(update.updated_at)

        assert activity, updates.updates
        assert activity == sorted(activity)

        await service.sessions_drop_to(0)
        assert await mode.history.rows(session_id) > 0

        deleter = ThreadDeleter(mode.launcher, ThreadAgentsIndex())
        await deleter.thread_deleted(await mode.stored(identity.thread_id))

        await service.sessions_drop_to(0)
        assert await mode.history.rows(session_id) == 0

    async def test_deletion_needs_no_chat_profile_in_the_config(
        self, mode: ServiceModeStand, service: AgentServiceStand
    ) -> None:
        """Профиль треда ушёл из [profiles] чата: удаление всё равно входит в
        службу от имени владельца и стирает сессию — модели чата службе не
        нужны."""
        identity, session_id = await mode.opened(RecordingUpdates())
        await service.sessions_drop_to(0)
        assert await mode.history.rows(session_id) > 0

        other = ChatProfiles({"other": mode.profile()})
        launcher = mode.launcher_of(other, mode.config)
        deleter = ThreadDeleter(launcher, ThreadAgentsIndex())
        await deleter.thread_deleted(await mode.stored(identity.thread_id))

        await service.sessions_drop_to(0)
        assert await mode.history.rows(session_id) == 0

    async def test_two_tabs_of_one_user_take_turns(
        self, mode: ServiceModeStand, threads: ThreadsTable
    ) -> None:
        """Две вкладки alice — два подключения к службе: серверы чата каждой
        отвечают своей сессии, ходы вкладок поочерёдно доходят до idle."""
        updates = (RecordingUpdates(), RecordingUpdates())
        tabs: list[ThreadAgent] = []
        for recorded in updates:
            identity = await mode.identity(SignInStand.PROFILE)
            meta = AgentSessionMeta(threads)
            tabs.append(
                ThreadAgent(identity, mode.launcher, meta, recorded, mode.menus)
            )

        try:
            await tabs[0].start()
            await tabs[0].ask(Question(key="a1", text=ScenarioName.ANSWER.value))
            await tabs[1].start()
            for key in ("a2", "a3"):
                for tab in tabs:
                    question = Question(
                        key=f"{key}-{tab.thread_id}", text=ScenarioName.ANSWER.value
                    )
                    await asyncio.wait_for(tab.ask(question), 60.0)
        finally:
            for tab in tabs:
                await tab.close()

        assert len(updates[0].finished) == 3, updates[0].updates
        assert len(updates[1].finished) == 2, updates[1].updates

    async def test_profile_the_service_does_not_offer_is_refused(
        self, mode: ServiceModeStand, threads: ThreadsTable
    ) -> None:
        """Профиль, которого служба не предлагает, отказан уже открытием
        вкладки: она открывает сессию сразу."""
        identity = await mode.identity(FOREIGN_PROFILE)
        agent = ThreadAgent(
            identity,
            mode.launcher,
            AgentSessionMeta(threads),
            RecordingUpdates(),
            mode.menus,
        )
        try:
            with pytest.raises(AgentProcessError, match="is not among them"):
                await agent.start()
        finally:
            await agent.close()


class TestLongTurn:
    """Вопрос во время хода служба вставляет своим ходом после идущего хода и
    только тогда отвечает на его `session/prompt`: ход дольше
    request_timeout_sec не обрывает ни себя, ни вопрос очереди. Служба,
    ушедшая посреди хода, — отказ обрывом соединения у обоих."""

    REQUEST_TIMEOUT_SEC: ClassVar[float] = 1.0
    HELD_SEC: ClassVar[float] = 3.0
    """Сколько ход стоит на паузе модели: дольше срока запроса."""
    TURN_SEC: ClassVar[float] = 60.0
    HELD_ANSWER: ClassVar[str] = "Copy the schema, then move the data."
    QUEUED_ANSWER: ClassVar[str] = "About an hour on the stand."

    def _book(self, model: FakeModel, held: str, queued: str) -> ReplyBookClient:
        book = ReplyBookClient(StandUrl.of(model.port))
        book.register(
            [
                Reply(
                    question=held,
                    turns=[
                        ReplyTurn(
                            content=self.HELD_ANSWER, hold=True, hold_after_tokens=2
                        )
                    ],
                ),
                Reply(question=queued, turns=[ReplyTurn(content=self.QUEUED_ANSWER)]),
            ]
        )

        return book

    async def _tab(
        self, mode: ServiceModeStand, threads: ThreadsTable, updates: RecordingUpdates
    ) -> ThreadAgent:
        """Вкладка, чей чат ждёт ответа службы на метод сессии
        REQUEST_TIMEOUT_SEC."""
        identity = await mode.identity(SignInStand.PROFILE)
        profiles = ChatProfiles({SignInStand.PROFILE: mode.profile()})
        config = mode.answering_within(self.REQUEST_TIMEOUT_SEC)
        launcher = mode.launcher_of(profiles, config)
        agent = ThreadAgent(
            identity, launcher, AgentSessionMeta(threads), updates, mode.menus
        )
        await agent.start()

        return agent

    async def test_a_question_during_a_long_turn_gets_its_turn(
        self, mode: ServiceModeStand, model: FakeModel, threads: ThreadsTable
    ) -> None:
        held = f"Describe the migration {uuid4()}"
        queued = f"And how long will it take {uuid4()}"
        book = await asyncio.to_thread(self._book, model, held, queued)
        updates = RecordingUpdates()
        agent = await self._tab(mode, threads, updates)
        queued_key = str(uuid4())
        try:
            first = asyncio.ensure_future(agent.ask(Question(key="held", text=held)))
            await asyncio.to_thread(book.await_held, held, self.TURN_SEC)
            second = asyncio.ensure_future(
                agent.ask(Question(key=queued_key, text=queued))
            )
            await asyncio.sleep(self.HELD_SEC)
            await asyncio.to_thread(book.release, held)
            await asyncio.wait_for(asyncio.gather(first, second), self.TURN_SEC)
        finally:
            await agent.close()

        assert updates.inserted_questions == [queued_key], updates.updates
        joined = "".join(updates.texts())
        assert self.HELD_ANSWER in joined, joined
        assert self.QUEUED_ANSWER in joined, joined
        assert joined.index(self.HELD_ANSWER) < joined.index(self.QUEUED_ANSWER)
        assert updates.finished[-1][1].stop_reason == "end_turn", updates.finished

    async def test_the_service_leaving_mid_turn_is_a_lost_connection(
        self,
        mode: ServiceModeStand,
        model: FakeModel,
        threads: ThreadsTable,
        service: AgentServiceStand,
    ) -> None:
        held = f"Describe the migration {uuid4()}"
        queued = f"And how long will it take {uuid4()}"
        book = await asyncio.to_thread(self._book, model, held, queued)
        agent = await self._tab(mode, threads, RecordingUpdates())
        try:
            first = asyncio.ensure_future(agent.ask(Question(key="held", text=held)))
            await asyncio.to_thread(book.await_held, held, self.TURN_SEC)
            second = asyncio.ensure_future(
                agent.ask(Question(key=str(uuid4()), text=queued))
            )
            await asyncio.sleep(self.HELD_SEC)
            await service.outage()
            outcomes = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), self.TURN_SEC
            )
        finally:
            await asyncio.to_thread(book.release, held)
            await agent.close()

        for outcome in outcomes:
            assert isinstance(outcome, AgentProcessError), outcomes
            assert outcome.failure.kind is AgentFailureKind.CONNECTION_LOST, outcomes
            assert "request_timeout_sec" not in str(outcome), outcomes
