"""Агент треда в chainlit: процесс на вкладку, id сессии в threads.meta, ход
и его остановка, перезапуск процесса после правки настроек, удаление сессии с
тредом; поток сессии уходит приёмнику TurnRenderer, который создаёт фабрика
TurnRenderers (feed.py).

Ошибки:
AcpClientError и наследники — процесс или ответ агента (client.py).
AgentSettingsError — профиль чата не годится агенту (settings.py).
DataUnavailableError — таблицы чата недоступны.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeAlias
from urllib.parse import urljoin
from uuid import UUID

from acp.experimental.v2 import schema
from pydantic import AnyUrl, BaseModel, ConfigDict, Field

import chainlit as cl
from boba.canvas.keys import ElementProps
from boba.chainlit.agent.client import (
    AcpClientError,
    AgentClient,
    AgentCommand,
    AgentProcess,
    AgentProcessError,
    AgentRequestError,
    PromptBlock,
    SessionUpdate,
    SessionUpdates,
)
from boba.chainlit.agent.config import AgentConfig
from boba.chainlit.agent.relay import McpRelay
from boba.chainlit.agent.server import KeyHolder, ProcessKeys
from boba.chainlit.agent.settings import (
    AgentSettingsChange,
    AgentSettingsLayers,
    AgentSettingsTable,
    AgentWorkdirs,
    ChatAgentSettings,
    HistoryEnvironment,
    SettingsFileBuilder,
)
from boba.chainlit.chat.feed import QuestionBody, ShownElement
from boba.chainlit.data.data_layer import ThreadDeletionListener
from boba.chainlit.domain.fields import ThreadMetaField
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.infra.session import ChainlitSession, LiveSignIn
from boba.chat.profiles import SelectedProfile
from boba.chat.threads import ThreadUpsert
from boba.identity.errors import InternalServiceError
from boba.runtime.threads import ThreadsTable
from boba.transport.acp.protocol import AcpErrorCode, MetaKey

__all__ = [
    "AgentLaunch",
    "AgentLauncher",
    "AgentSessionMeta",
    "AgentSettingsChanges",
    "ConfigCategory",
    "ConfigOption",
    "LaunchedAgent",
    "MuteUpdates",
    "PermissionKind",
    "Question",
    "SessionOptions",
    "SettingsOutcome",
    "ThreadAgent",
    "ThreadAgents",
    "ThreadAgentsIndex",
    "ThreadDeleter",
    "ThreadIdentity",
    "TurnRenderer",
    "TurnRenderers",
]

logger = logging.getLogger(__name__)

ConfigOption: TypeAlias = (
    schema.SelectSessionConfigOption
    | schema.BooleanSessionConfigOption
    | schema.OtherSessionConfigOption
)


class Question(BaseModel):
    """Вопрос пользователя, с которого начинается ход: id его шага, текст и
    вложения к сообщению.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    text: str
    elements: tuple[ShownElement, ...] = ()

    @classmethod
    def of_message(
        cls, msg: cl.Message, thread_id: str, links: AttachmentLinks
    ) -> Question:
        """Вопрос из сообщения chainlit: вложения получают ссылку треда вместо
        сессионной, чтобы открываться из любой вкладки и с любого инстанса.
        """
        attached = msg.elements
        if not attached:
            attached = []

        elements: list[ShownElement] = []
        for element in attached:
            data = dict(element.to_dict())
            props = ElementProps.of(data.get("props"))
            data["threadId"] = thread_id
            data["forId"] = msg.id
            data["chainlitKey"] = None
            data["url"] = links.url(thread_id, element.id, props.dir)
            elements.append(ShownElement.model_validate(data))

        return cls(key=msg.id, text=msg.content, elements=tuple(elements))

    def body(self, author: str = "") -> QuestionBody:
        """Тело вопроса для шины; author — имя шага вопроса во всех вкладках."""
        return QuestionBody(text=self.text, elements=self.elements, author=author)


class PermissionKind(StrEnum):
    """Виды вариантов ответа на вопрос разрешения."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"
    REJECT_ALWAYS = "reject_always"


@dataclass(frozen=True)
class ThreadIdentity:
    """Кто и в каком треде говорит с агентом: тред, пользователь, его вход и
    профиль чата. Собирается из сессии chainlit в обработчике. session —
    живой вход пользователя вкладки: по нему держатель ключа процесса
    читает роли и секреты входа в момент вызова."""

    thread_id: str
    user_id: UUID
    login: str
    roles: frozenset[str]
    session: LiveSignIn
    profile: SelectedProfile

    @classmethod
    def of(cls, session: ChainlitSession, profile: SelectedProfile) -> ThreadIdentity:
        """Ошибки:
        InternalServiceError — у сессии нет треда или сохранённого пользователя.
        AuthenticationError — токен входа отсутствует или не принят.
        """
        thread_id = session.thread_id
        if not thread_id:
            raise InternalServiceError(
                internal_detail=(
                    f"agent of session {session.id!r}: the session has no thread"
                ),
                user_detail=None,
            )

        user_id = session.user_id
        if not user_id:
            raise InternalServiceError(
                internal_detail=(
                    f"agent of thread {thread_id}: sign-in {session.label!r} "
                    "has no users row"
                ),
                user_detail=None,
            )

        return cls(
            thread_id=thread_id,
            user_id=UUID(user_id),
            login=session.identifier,
            roles=session.roles,
            session=session,
            profile=profile,
        )


class AgentSessionMeta:
    """Соответствие тред → sessionId агента в threads.meta: его читают
    открытие треда и удаление, пишут только открытие новой сессии и ветка
    при правке вопроса; слой данных этот ключ от chainlit не принимает."""

    def __init__(self, threads: ThreadsTable) -> None:
        self._threads = threads

    async def session_of(self, thread_id: str) -> str:
        """Id сессии агента для загрузки: из metadata треда, а без записи —
        id самого треда: так названы сессии мигрированных тредов, и так
        их находит `session/resume`; неизвестную сессию агент отвергнет сам."""
        stored = await self._threads.get(UUID(thread_id))
        if stored is None:
            return thread_id

        known = self.in_meta(stored.meta)
        if known is None:
            return thread_id

        return known

    @staticmethod
    def in_meta(meta: Any) -> str | None:
        """Id сессии агента в metadata треда; None — его там нет."""
        if not isinstance(meta, dict):
            return None

        value = meta.get(ThreadMetaField.AGENT_SESSION)
        if not isinstance(value, str):
            return None

        if not value:
            return None

        return value

    async def remember(self, thread_id: str, session_id: str) -> None:
        change = ThreadUpsert(
            id=UUID(thread_id), meta_set={ThreadMetaField.AGENT_SESSION: session_id}
        )
        await self._threads.upsert(change)


class SessionOptions:
    """Текущие настройки сессии агента (`configOptions`): их отдают ответы
    `session/new|resume|fork`, присылает уведомление
    `config_option_update`; читает панель настроек чата. Один объект на
    агента треда, общий у него и у приёмника потока."""

    def __init__(self) -> None:
        self._options: list[ConfigOption] = []

    @property
    def options(self) -> Sequence[ConfigOption]:
        return list(self._options)

    def replace(self, options: Sequence[ConfigOption] | None) -> None:
        """Полное состояние настроек; None — ответ без них (наблюдатель)."""
        if options is None:
            return

        self._options = list(options)

    def selects(self) -> Sequence[schema.SelectSessionConfigOption]:
        """Настройки-списки: режим и модель."""
        found: list[schema.SelectSessionConfigOption] = []
        for option in self._options:
            if isinstance(option, schema.SelectSessionConfigOption):
                found.append(option)

        return found

    def current(self, config_id: str) -> str | None:
        """Текущее значение настройки-списка; None — такой настройки нет."""
        for option in self.selects():
            if option.config_id == config_id:
                return option.current_value

        return None


class ConfigCategory(StrEnum):
    """Категории настроек сессии, которые различает панель."""

    MODE = "mode"
    MODEL = "model"


class TurnRenderer(SessionUpdates, Protocol):
    """Приёмник потока сессии, который знает и о ходе вкладки: его начало,
    остановку и конец воспроизведения истории. Реализует AgentFeed (feed.py).
    """

    @abstractmethod
    def stopping(self, session_id: str) -> None:
        """Ход останавливается: висящие вопросы разрешения получат отмену."""

    @abstractmethod
    async def turn_started(
        self, session_id: str, question: Question, prompt: Coroutine[Any, Any, str]
    ) -> str:
        """Ход по вопросу начинается: prompt — ещё не начатый `session/prompt`,
        его ждёт приёмник; итог — messageId, который агент дал вопросу, он же
        ключ хода.

        Ошибки:
        LockBusyError — тред уже ведёт другой держатель.
        AcpClientError — агент не принял вопрос.
        """

    @abstractmethod
    async def loaded(self, session_id: str) -> None:
        """`session/resume` с воспроизведением ответил: история нарисована."""


class TurnRenderers(Protocol):
    """Фабрика приёмников потока для агентов вкладки; реализует AgentFeeds."""

    @abstractmethod
    async def create(
        self, identity: ThreadIdentity, options: SessionOptions
    ) -> TurnRenderer:
        """Приёмник для агента треда identity с общим состоянием настроек.

        Ошибки:
        McpClientError — серверы MCP профиля не открылись.
        """


class MuteUpdates(SessionUpdates):
    """Приёмник без ленты: короткоживущий процесс удаления сессии."""

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        return

    async def permission(
        self, session_id: str, title: str, options: Sequence[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        return schema.RequestPermissionResponse(
            outcome=schema.CancelledPermissionOutcome()
        )

    async def turn_finished(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        return

    async def abandon(self, session_id: str) -> None:
        return


class AskedQuestions(SessionUpdates):
    """Приёмник-обёртка агента треда: запоминает id вопросов, которые агент
    воспроизвёл (`user_message.messageId`), решает ожидание конца хода по
    idle и передаёт поток дальше рендереру. По id вопросов агент треда
    узнаёт правку вопроса после возврата к треду."""

    def __init__(self, inner: SessionUpdates) -> None:
        self._inner = inner
        self.ids: list[str] = []
        self._ending: dict[str, asyncio.Future[schema.IdleSessionStateUpdate]] = {}
        """Ожидание конца хода по сессии; ждут одну сессию, idle снимает его."""

    def ending(self, session_id: str) -> asyncio.Future[schema.IdleSessionStateUpdate]:
        """Ожидание конца хода сессии session_id: его решает idle агента.
        Ставится до `session/prompt`, чтобы idle не прошёл мимо; заменяет
        прежнее ожидание."""
        ending: asyncio.Future[schema.IdleSessionStateUpdate]
        ending = asyncio.get_running_loop().create_future()
        self._ending.clear()
        self._ending[session_id] = ending

        return ending

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        if isinstance(update, schema.UserMessageUpdate):
            if update.message_id not in self.ids:
                self.ids.append(update.message_id)

        if isinstance(update, schema.IdleSessionStateUpdate):
            ended = self._ending.pop(session_id, None)
            if ended is not None:
                ended.set_result(update)

        await self._inner.update(session_id, update)

    async def permission(
        self, session_id: str, title: str, options: Sequence[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        return await self._inner.permission(session_id, title, options)

    async def turn_finished(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        await self._inner.turn_finished(session_id, idle)

    async def abandon(self, session_id: str) -> None:
        await self._inner.abandon(session_id)


@dataclass(frozen=True)
class AgentLaunch:
    """Заготовка запуска агента вкладки: слой чата, из которого соберётся файл
    настроек. Собирает AgentLauncher.prepare до остановки прежнего процесса."""

    chat: ChatAgentSettings


@dataclass(frozen=True)
class LaunchedAgent:
    """Запущенный процесс агента и серверы, объявленные ему записями type
    "acp": serverId каждого — ключ процесса в ProcessKeys. Отдаёт
    AgentLauncher, держит ThreadAgent и возвращает запуску на снятие ключей
    (AgentLauncher.retire)."""

    process: AgentProcess
    servers: Sequence[schema.AcpMcpServer]


class AgentLauncher:
    """Запуск процесса агента под тред: слой чата из Postgres → регистрации
    серверов профиля и сервера чата → файл настроек в приватном каталоге →
    процесс → рукопожатие.

    Создаётся провайдером процесса; зовут ThreadAgent и ThreadDeleter.
    Регистрации выдаёт и снимает реестр ProcessKeys: каждый запуск получает
    новые serverId, остановка процесса и несостоявшийся запуск их снимают.
    Запросы mcp/message процесса отвечает общий ретранслятор (McpRelay).
    """

    def __init__(
        self,
        config: AgentConfig,
        layers: AgentSettingsLayers,
        keys: ProcessKeys,
        relay: McpRelay,
        version: str,
    ) -> None:
        self._config = config
        self._layers = layers
        self._keys = keys
        self._relay = relay
        self._files = SettingsFileBuilder(config)
        self._workdirs = AgentWorkdirs(Path(config.process.private_dir))
        self._environment = HistoryEnvironment(config.history)
        self._command = AgentCommand(
            executable=config.process.command,
            args=tuple(config.process.args),
            env=dict(config.process.env),
            start_timeout_sec=config.process.start_timeout_sec,
            stop_timeout_sec=config.process.stop_timeout_sec,
        )
        self._version = version

    async def prepare(self, identity: ThreadIdentity) -> AgentLaunch:
        """Слой чата треда (у нового треда копируется из слоя пользователя):
        то, что может отказать до остановки прежнего процесса.

        Ошибки:
        AgentSettingsError — профиль не годится агенту.
        DataUnavailableError — слой настроек недоступен.
        """
        chat = await self._layers.chat_layer(
            UUID(identity.thread_id),
            identity.user_id,
            identity.profile.name,
            identity.profile.config,
        )

        return AgentLaunch(chat=chat)

    async def launch(
        self, identity: ThreadIdentity, chat: ChatAgentSettings, client: AgentClient
    ) -> LaunchedAgent:
        """Процесс под вкладку треда по слою чата: серверы профиля и сервер
        чата регистрируются от имени пользователя вкладки новыми serverId.

        Ошибки:
        AgentProcessError — процесс не запустился.
        """
        names = list(identity.profile.config.mcp)
        if self._config.chat_mcp.enable:
            names.append(self._config.chat_mcp.name)

        holder = KeyHolder(
            user_id=identity.user_id,
            login=identity.login,
            thread_id=identity.thread_id,
            profile=identity.profile.name,
            session=identity.session,
            server=self._config.chat_mcp.name,
        )
        servers = self._keys.register(holder, names)
        try:
            process = await self.launch_settings(
                identity.thread_id, chat, servers, client
            )
        except Exception:
            for server in servers:
                self._keys.revoke(server.server_id)

            raise

        return LaunchedAgent(process=process, servers=servers)

    def retire(self, launched: LaunchedAgent) -> None:
        """Снимает регистрации остановленного процесса: ретранслятор, сервер
        чата и хук их больше не принимают."""
        for server in launched.servers:
            self._keys.revoke(server.server_id)

    async def launch_settings(
        self,
        thread_id: str,
        chat: ChatAgentSettings,
        servers: Sequence[schema.AcpMcpServer],
        client: AgentClient,
    ) -> AgentProcess:
        """Процесс по готовому слою чата с объявлениями серверов servers;
        kerberos-роль истории получает окружение libkrb5.

        Ошибки:
        AgentProcessError — процесс не запустился.
        KerberosError — билет истории по keytab не получен.
        """
        document = self._files.build(chat)
        workdir = self._workdirs.create(thread_id)
        workdir.write(document, chat.system_prompt)
        command = self._command.with_env(await self._environment.prepare())

        process = AgentProcess(
            command, workdir, client, self._relay, servers, self._version
        )
        await process.start()

        return process


@dataclass(frozen=True)
class StartedTurn:
    """Начатый ход вкладки: сессия агента и ожидание её idle."""

    session_id: str
    ending: asyncio.Future[schema.IdleSessionStateUpdate]


class SettingsOutcome(StrEnum):
    """Что стало с процессом агента вкладки после правки настроек."""

    RESTARTED = "restarted"
    DEFERRED = "deferred"
    SAVED = "saved"


class ThreadAgent:
    """Агент одной вкладки треда: процесс, сессия агента и текущий ход.

    Создаётся реестром ThreadAgents; живёт пока открыта вкладка. Сессия
    открывается `session/new` на первом вопросе или `session/resume` с
    воспроизведением при возврате к треду; правка вопроса ветвит сессию
    `session/fork`. Ход кончается idle агента (AskedQuestions.ending) либо
    выходом процесса. Процесс заменяется новым только через restart(): запуск,
    смена процесса и начало хода идут под одним замком вкладки, а перезапуск,
    пришедший во время хода, ждёт его конца флагом.
    """

    def __init__(  # noqa: PLR0913 — агент вкладки собирается всеми входами
        self,
        identity: ThreadIdentity,
        launcher: AgentLauncher,
        meta: AgentSessionMeta,
        renderer: TurnRenderer,
        options: SessionOptions,
        public_url: str,
    ) -> None:
        self._identity = identity
        self._launcher = launcher
        self._meta = meta
        self._renderer = renderer
        self._options = options
        self._public_url = public_url
        self._questions = AskedQuestions(renderer)
        self._launched: LaunchedAgent | None = None
        self._session_id: str | None = None
        self._asked: list[str] = []
        self._active: str | None = None
        self._gate = asyncio.Lock()
        self._restart_pending = False
        self._restarts = 0

    @property
    def thread_id(self) -> str:
        return self._identity.thread_id

    @property
    def session_id(self) -> str | None:
        """Id сессии агента; None — сессия ещё не открыта."""
        return self._session_id

    @property
    def options(self) -> SessionOptions:
        """Настройки открытой сессии; у закрытой — пустые."""
        return self._options

    @property
    def restarts(self) -> int:
        """Сколько раз процесс заменён новым: по счётчику обработчик хода
        узнаёт, что отложенный перезапуск состоялся."""
        return self._restarts

    @property
    def process(self) -> AgentProcess:
        if self._launched is None:
            msg = f"agent of thread {self.thread_id}: the process is not started"
            raise InternalServiceError(internal_detail=msg, user_detail=None)

        return self._launched.process

    def turn_active(self) -> bool:
        return self._active is not None

    async def start(self) -> None:
        """Процесс агента; сессия открывается позже."""
        async with self._gate:
            await self._launch(await self._launcher.prepare(self._identity))

    async def restart(self) -> bool:
        """Единая точка перезапуска процесса агента вкладки: файл настроек
        собирается заново из слоя чата, прежний процесс закрывается, новый
        запускается и открывает сессию треда без воспроизведения ленты
        (`session/resume`). False — идёт ход: перезапуск отложен и состоится
        сразу после его конца. Повторный вызов до отложенного перезапуска
        ничего не добавляет.

        Ошибки:
        AgentSettingsError, DataUnavailableError — слой чата не собран.
        AgentProcessError, AgentRequestError — процесс не запустился или
            отказал в сессии; перезапуск повторится перед следующим ходом.
        """
        async with self._gate:
            return await self._restart_idle()

    async def _restart_idle(self) -> bool:
        """Перезапуск под замком вкладки; при идущем ходе только ставит флаг."""
        self._restart_pending = True
        if self._active is not None:
            logger.info(
                "thread %s: restart waits for the end of the turn", self.thread_id
            )

            return False

        launch = await self._launcher.prepare(self._identity)
        await self._close_process()
        await self._launch(launch)
        await self._resume(replay=False)
        self._restart_pending = False
        self._restarts += 1
        logger.info(
            "thread %s: agent restarted, pid=%s, session=%s",
            self.thread_id,
            self.process.pid,
            self._session_id,
        )

        return True

    async def _launch(self, launch: AgentLaunch) -> None:
        """Новый процесс по заготовке: файл настроек из слоя чата в свежем
        приватном каталоге, новые регистрации серверов, рукопожатие."""
        self._launched = await self._launcher.launch(
            self._identity, launch.chat, AgentClient(self._questions)
        )

    async def resume(self) -> bool:
        """Сохранённая сессия треда с воспроизведением беседы; False — у
        агента нет такой сессии, первый вопрос откроет новую.

        Ошибки:
        AgentRequestError — агент отказал в `session/resume` не по причине
            неизвестной сессии.
        """
        async with self._gate:
            return await self._resume(replay=True)

    async def _resume(self, replay: bool) -> bool:
        """Сессия треда в процессе вкладки: с воспроизведением беседы — при
        возврате к треду (id из threads.meta), без него — открытая сессия в
        новом процессе после перезапуска; ответ без настроек их не меняет.
        False — сессии нет: открытой нет или агент её не знает, и первый
        вопрос откроет новую."""
        known = self._session_id
        if replay:
            known = await self._meta.session_of(self.thread_id)

        if known is None:
            return False

        try:
            response = await self.process.resume_session(known, replay)
        except AgentRequestError as exc:
            if exc.code != AcpErrorCode.RESOURCE_NOT_FOUND:
                raise

            logger.info(
                "thread %s: no agent session %s yet: %s", self.thread_id, known, exc
            )
            self._session_id = None

            return False

        self._session_id = known
        self._options.replace(response.config_options)
        if not replay:
            return True

        self._asked = list(self._questions.ids)
        await self._renderer.loaded(known)
        logger.info(
            "thread %s: agent session %s resumed, %d question(s) replayed",
            self.thread_id,
            known,
            len(self._asked),
        )

        return True

    async def ask(self, question: Question) -> None:
        """Ход по вопросу: новая сессия на первом, ветвь на правке; ход
        кончается idle агента. Кнопка Stop снимает задачу обработчика chainlit: ход
        всё равно дожидается idle агента после отмены и отмечает его в ленте.
        Отложенный перезапуск исполняется сразу после конца хода, а не
        состоявшийся из-за отказа — перед началом следующего.

        Ошибки:
        AgentRequestError — агент отказал в методе сессии.
        AgentProcessError — процесс умер по ходу.
        LockBusyError — тред уже ведёт другой держатель.
        """
        async with self._gate:
            turn = await self._begin(question)

        session_id = turn.session_id
        try:
            idle = await self._idle(turn)
        except asyncio.CancelledError:
            if not turn.ending.done():
                self._renderer.stopping(session_id)
                await self.process.cancel(session_id)

            idle = await self._idle(turn)
            await self._finish(session_id, idle)
            raise
        except Exception:
            self._active = None
            await self._renderer.abandon(session_id)
            raise

        await self._finish(session_id, idle)

    async def _begin(self, question: Question) -> StartedTurn:
        """Начало хода под замком вкладки: сессия, ожидание её idle, вопрос
        агенту; ключ хода — messageId, который агент дал вопросу."""
        if self._restart_pending:
            await self._restart_idle()

        session_id = await self._session_for(question)
        ending = self._questions.ending(session_id)
        prompt = self.process.prompt(session_id, self._blocks(question))
        key = await self._renderer.turn_started(session_id, question, prompt)
        self._asked.append(key)
        self._active = session_id
        logger.info(
            "thread %s: turn %s starts in session %s", self.thread_id, key, session_id
        )

        return StartedTurn(session_id=session_id, ending=ending)

    async def _idle(self, turn: StartedTurn) -> schema.IdleSessionStateUpdate:
        """idle хода либо выход процесса, который idle уже не пришлёт.

        Ошибки:
        AgentProcessError — процесс вышел посреди хода.
        """
        exited = asyncio.ensure_future(self.process.exited())
        try:
            await asyncio.wait(
                (turn.ending, exited), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            exited.cancel()

        if turn.ending.done():
            return turn.ending.result()

        msg = (
            f"thread {self.thread_id}: the agent process exited during the turn "
            f"of session {turn.session_id}: {self.process.log_tail()}"
        )
        raise AgentProcessError(msg)

    async def _finish(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        self._active = None
        await self._renderer.turn_finished(session_id, idle)
        logger.info(
            "thread %s: turn of session %s finished with %s",
            self.thread_id,
            session_id,
            idle.stop_reason,
        )
        await self._restart_deferred()

    async def _restart_deferred(self) -> None:
        """Перезапуск, отложенный на время хода; без флага — ничего."""
        async with self._gate:
            if not self._restart_pending:
                return

            await self._restart_idle()

    async def stop(self) -> bool:
        """Отмена идущего хода; False — хода нет."""
        active = self._active
        if active is None:
            return False

        self._renderer.stopping(active)
        await self.process.cancel(active)
        logger.info("thread %s: cancel sent for session %s", self.thread_id, active)

        return True

    async def close(self) -> None:
        """Закрытие сессии у агента (замок отпущен) и остановка процесса."""
        async with self._gate:
            await self._close_process()

    async def _close_process(self) -> None:
        """Прежний процесс: сессия закрыта у агента, процесс остановлен, его
        каталог с файлом настроек удалён, регистрации серверов сняты."""
        launched = self._launched
        if launched is None:
            return

        self._launched = None
        try:
            await self._close_session(launched.process)
        finally:
            try:
                await launched.process.close()
            finally:
                self._launcher.retire(launched)

    async def _close_session(self, process: AgentProcess) -> None:
        session_id = self._session_id
        if session_id is None:
            return

        if not process.alive():
            return

        await process.close_session(session_id)
        logger.info("thread %s: agent session %s closed", self.thread_id, session_id)

    async def _session_for(self, question: Question) -> str:
        """Сессия хода: открытая, ветвь до вопроса при его правке, иначе новая."""
        current = self._session_id
        if current is None:
            opened = await self.process.open_session()
            created = opened.session_id
            self._options.replace(opened.config_options)
            await self._meta.remember(self.thread_id, created)
            self._session_id = created
            logger.info("thread %s: agent session %s created", self.thread_id, created)

            return created

        if question.key not in self._asked:
            return current

        if not self.process.forks_by_message():
            msg = (
                f"The question {question.key} of thread {self.thread_id} was "
                "edited, but the agent of this chat does not declare "
                f"{MetaKey.UP_TO_MESSAGE_ID.value!r} in "
                "capabilities.session.fork._meta, so it cannot branch the "
                "conversation before the edited question; ask the question "
                "anew as a new message instead of editing it."
            )
            raise AcpClientError(msg)

        branch = await self.process.fork(current, question.key)
        forked = branch.session_id
        self._options.replace(branch.config_options)
        await self._meta.remember(self.thread_id, forked)
        self._session_id = forked
        logger.info(
            "thread %s: question %s edited, session %s forked into %s",
            self.thread_id,
            question.key,
            current,
            forked,
        )

        return forked

    def _blocks(self, question: Question) -> list[PromptBlock]:
        """Текст вопроса и ссылки на вложения по адресу chainlit глазами
        агента; вложение без адреса агенту не передаётся."""
        blocks: list[PromptBlock] = [schema.TextContentBlock(text=question.text)]
        for element in question.elements:
            if element.url is None:
                continue

            uri = AnyUrl(urljoin(self._public_url, element.url))
            blocks.append(
                schema.ResourceContentBlock(
                    name=element.name, uri=uri, mime_type=element.mime
                )
            )

        return blocks


class ThreadAgentsIndex:
    """Агенты всех вкладок инстанса по тредам: удаление треда закрывает их
    первыми, чтобы сессия агента не осталась под замком этого процесса."""

    def __init__(self) -> None:
        self._agents: dict[str, list[ThreadAgent]] = {}

    def register(self, agent: ThreadAgent) -> None:
        self._agents.setdefault(agent.thread_id, []).append(agent)

    def forget(self, agent: ThreadAgent) -> None:
        agents = self._agents.get(agent.thread_id)
        if agents is None:
            return

        if agent in agents:
            agents.remove(agent)

        if not agents:
            self._agents.pop(agent.thread_id, None)

    def of_thread(self, thread_id: str) -> Sequence[ThreadAgent]:
        return list(self._agents.get(thread_id, ()))

    def active(self, thread_id: str) -> ThreadAgent | None:
        """Агент треда с идущим ходом в любой вкладке инстанса; None — хода нет."""
        for agent in self.of_thread(thread_id):
            if agent.turn_active():
                return agent

        return None

    async def close_thread(self, thread_id: str) -> int:
        """Закрывает агентов треда во всех вкладках инстанса; итог — сколько."""
        agents = self.of_thread(thread_id)
        for agent in agents:
            self.forget(agent)
            await agent.close()

        return len(agents)


class ThreadAgents:
    """Агенты вкладки chainlit по тредам: один процесс на открытый тред;
    закрывается с DI-контейнером сессии."""

    def __init__(
        self,
        launcher: AgentLauncher,
        meta: AgentSessionMeta,
        index: ThreadAgentsIndex,
        renderers: TurnRenderers,
        public_url: str,
    ) -> None:
        self._launcher = launcher
        self._meta = meta
        self._index = index
        self._renderers = renderers
        self._public_url = public_url
        self._agents: dict[str, ThreadAgent] = {}
        self._starting = asyncio.Lock()

    def of_thread(self, thread_id: str) -> ThreadAgent | None:
        """Агент треда этой вкладки; None — тред во вкладке не открывался."""
        return self._agents.get(thread_id)

    async def of(self, identity: ThreadIdentity) -> ThreadAgent:
        """Агент треда; при первом обращении процесс запускается. Старт вкладки
        и первый вопрос приходят почти разом — второй ждёт первого под замком."""
        async with self._starting:
            known = self._agents.get(identity.thread_id)
            if known is not None:
                return known

            options = SessionOptions()
            renderer = await self._renderers.create(identity, options)
            agent = ThreadAgent(
                identity,
                self._launcher,
                self._meta,
                renderer,
                options,
                self._public_url,
            )
            await agent.start()
            self._agents[identity.thread_id] = agent
            self._index.register(agent)

            return agent

    def active(self, thread_id: str) -> ThreadAgent | None:
        """Агент треда с идущим ходом; None — хода в этой вкладке нет."""
        agent = self._agents.get(thread_id)
        if agent is None:
            return None

        if not agent.turn_active():
            return None

        return agent

    async def close_all(self) -> None:
        agents = list(self._agents.values())
        self._agents.clear()
        for agent in agents:
            self._index.forget(agent)
            try:
                await agent.close()
            except Exception:
                logger.exception("thread %s: closing the agent failed", agent.thread_id)


class AgentSettingsChanges:
    """Единый путь правки настроек агента в чате: правка панели пишется в слой
    пользователя и в слой чата треда, после записи процесс агента вкладки
    всегда перезапускается, чтобы работать уже из нового слоя.

    Создаётся провайдером процесса; зовёт обработчик панели настроек. Слои
    пишет AgentSettingsLayers, процесс меняет ThreadAgent.restart.
    """

    def __init__(self, layers: AgentSettingsLayers) -> None:
        self._layers = layers

    async def apply(
        self,
        identity: ThreadIdentity,
        change: AgentSettingsChange,
        agent: ThreadAgent | None,
    ) -> SettingsOutcome:
        """Запись правки и перезапуск агента вкладки; agent None — во вкладке
        процесса нет, он запустится уже из нового слоя чата.

        Ошибки:
        AgentSettingsError, DataUnavailableError — слои не записаны.
        AgentProcessError, AgentRequestError — настройки записаны,
            перезапуск не состоялся.
        """
        await self._layers.change(
            UUID(identity.thread_id),
            identity.user_id,
            identity.profile.name,
            identity.profile.config,
            change,
        )
        if agent is None:
            return SettingsOutcome.SAVED

        if await agent.restart():
            return SettingsOutcome.RESTARTED

        return SettingsOutcome.DEFERRED


class ThreadDeleter(ThreadDeletionListener):
    """Удаление сессии агента вместе с тредом: агенты вкладок этого инстанса
    закрываются (отпускают замок), короткоживущий процесс под слоем чата
    делает `session/delete`, затем слой чата стирается.

    Реализация ThreadDeletionListener слоя данных; создаётся провайдером
    процесса и ставится на PostgresDataLayer в bootstrap. Сессию, которую
    держит другой инстанс, агент удалить не даст — тред остаётся.
    """

    def __init__(
        self,
        launcher: AgentLauncher,
        table: AgentSettingsTable,
        index: ThreadAgentsIndex,
    ) -> None:
        self._launcher = launcher
        self._table = table
        self._index = index

    async def thread_deleted(self, thread_id: str, meta: Mapping[str, Any]) -> None:
        """Ошибки:
        AgentRequestError — агент отказал в `session/delete`.
        AgentProcessError — процесс не запустился.
        DataUnavailableError — слой настроек недоступен.
        """
        closed = await self._index.close_thread(thread_id)
        if closed:
            logger.info(
                "thread %s: %d tab agent(s) closed before deletion", thread_id, closed
            )

        session_id = AgentSessionMeta.in_meta(meta)
        if session_id is None:
            session_id = thread_id

        chat = await self._table.chat_layer(UUID(thread_id))
        if chat is not None:
            await self._delete_session(thread_id, chat, session_id)

        await self._table.delete_chat_layer(UUID(thread_id))

    async def _delete_session(
        self, thread_id: str, chat: ChatAgentSettings, session_id: str
    ) -> None:
        process = await self._launcher.launch_settings(
            thread_id, chat, (), AgentClient(MuteUpdates())
        )
        try:
            await process.delete_session(session_id)
            logger.info("thread %s: agent session %s deleted", thread_id, session_id)
        finally:
            await process.close()
