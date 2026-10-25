"""Агент треда в чате aid: агент на вкладку — свой процесс или подключение к
службе агента ([agent.process]), id сессии в threads.meta, ход и его
остановка, замена вышедшего процесса или закрытого подключения, удаление
сессии с тредом; поток сессии уходит приёмнику TurnRenderer, который
создаёт фабрика TurnRenderers (feed.py). Команды «/» вкладки ведёт реестр
CommandMenu (commands.py): вопрос-команду он отдаёт владельцу, а команду,
которую исполнила вкладка, агент треда пишет в историю агента расширением
`_boba/session/record` — её эхо рисуется, как любое уведомление сессии.

Ошибки:
AcpClientError и наследники — процесс или ответ агента (client.py).
QuestionEditError — правка вопроса не принята (наследник AcpClientError).
AgentSettingsError — профиль чата не годится агенту или исчез из конфига
    (settings.py).
CommandError — команду сервера или чата не исполнить (commands.py).
DataUnavailableError — таблицы чата недоступны.
PostgresError — билет kerberos-роли истории для процесса агента не получен.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Collection, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin
from uuid import UUID, uuid4

import chainlit as cl
import mcp_types as mt
from acp.experimental.v2 import schema
from chainlit.session import WebsocketSession
from pydantic import AliasPath, AnyUrl, BaseModel, ConfigDict, Field

from boba.aid.agent.client import (
    AcpClientError,
    AgentClient,
    AgentCommand,
    AgentFailure,
    AgentFailureKind,
    AgentLink,
    AgentProcess,
    AgentProcessError,
    AgentRequestError,
    AgentService,
    ElicitationReply,
    LinkAction,
    PromptBlock,
    SessionUpdate,
    SessionUpdates,
)
from boba.aid.agent.commands import (
    CommandBoard,
    CommandMenu,
    CommandMenus,
    ExecutedCommand,
)
from boba.aid.agent.config import AgentCommandConfig, AgentConfig, AgentServiceConfig
from boba.aid.agent.relay import ChatTools, McpRegistration, McpRegistrations, McpRelay
from boba.aid.agent.settings import (
    AgentSettingsError,
    AgentWorkdirs,
    HistoryEnvironment,
    McpFile,
    ModelFile,
    ProfileModelSpecs,
    SettingsFileBuilder,
)
from boba.aid.chat.feed import QuestionBody
from boba.aid.data.data_layer import ThreadDeletionListener
from boba.aid.domain.fields import ThreadMetaField
from boba.aid.domain.keys import CanvasFileUrl
from boba.aid.infra.session import ChainlitSession, LiveSignIn
from boba.aid.rendering.renderer import ThreadRewinds
from boba.canvas.keys import ObjectKey
from boba.chat.profiles import ChatProfiles, SelectedProfile
from boba.chat.threads import DataLayerError, StoredThread, ThreadUpsert
from boba.identity.api import UserRows
from boba.identity.errors import InternalServiceError
from boba.mcp_client.client import McpCaller, McpServersConfig
from boba.runtime.threads import ThreadsTable
from boba.toolkit.result import BlockAudience, ContentMime, JsonBlock
from boba.transport.acp.protocol import (
    AcpErrorCode,
    MetaKey,
    SessionRecordRequest,
)

__all__ = [
    "AgentLaunch",
    "AgentLauncher",
    "AgentLinks",
    "AgentSessionMeta",
    "EditRefusal",
    "LaunchedAgent",
    "ModelContextUpdate",
    "MuteUpdates",
    "PermissionKind",
    "ProcessLinks",
    "Question",
    "QuestionEditError",
    "Questions",
    "ServiceLinks",
    "StartedTurn",
    "ThreadAgent",
    "ThreadAgents",
    "ThreadAgentsIndex",
    "ThreadDeleter",
    "ThreadIdentities",
    "ThreadIdentity",
    "TurnEnd",
    "TurnRenderer",
    "TurnRenderers",
]

logger = logging.getLogger(__name__)


class EditRefusal(StrEnum):
    """Почему правка вопроса не принята: по виду вкладка выбирает слова
    человеку (UserFailures в infra/notices.py)."""

    NOT_EDITED = "not-edited"
    """Прежнюю беседу не удалось заменить ветвью: всё осталось как было."""

    NOT_LOADED = "not-loaded"
    """Вопроса нет в беседе, которую видела вкладка."""

    NO_BRANCHING = "no-branching"
    """Агент не объявил ветвление беседы по сообщению."""


class QuestionEditError(AcpClientError):
    """Правка вопроса не принята; refusal — вид отказа для человека, текст
    исключения — подробность для журнала."""

    def __init__(self, message: str, refusal: EditRefusal) -> None:
        super().__init__(message)
        self.refusal = refusal


class Question(BaseModel):
    """Вопрос пользователя, с которого начинается ход: id его шага, текст,
    вложения к сообщению и признак правки. Строится из сообщения chainlit
    (Questions) или разбором события правки edit_message вкладки — по
    алиасам полей; edited — правка шага key: ход ведёт ветка `session/fork`
    до этого вопроса.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", validate_by_name=True)

    key: str = Field(min_length=1, validation_alias=AliasPath("message", "id"))
    text: str = Field(validation_alias=AliasPath("message", "output"))
    links: tuple[schema.ResourceContentBlock, ...] = ()
    edited: bool = False

    def body(self, author: str = "") -> QuestionBody:
        """Тело вопроса для шины; author — имя шага вопроса во всех вкладках."""
        return QuestionBody(text=self.text, links=self.links, author=author)


class Questions:
    """Сборка вопроса из сообщения chainlit: вложения — блоки resource_link с
    адресом файла по его месту в хранилище (маршрут canvas) глазами агента.
    Держит её в поле обработчик хода вкладки (TabAgent)."""

    def of_message(
        self, msg: cl.Message, identity: ThreadIdentity, public_url: str
    ) -> Question:
        """Вопрос из сообщения msg вкладки identity; public_url — адрес чата
        для агента."""
        attached = msg.elements
        if not attached:
            attached = []

        links: list[schema.ResourceContentBlock] = []
        for element in attached:
            key = ObjectKey.build(
                identity.user_id, identity.thread_id, element.name, element.id
            )
            mime = ContentMime.named(element.mime, element.name)

            path = CanvasFileUrl.path(key, mime)
            address = urljoin(public_url, path)
            uri = AnyUrl(address)
            links.append(
                schema.ResourceContentBlock(name=element.name, uri=uri, mime_type=mime)
            )

        return Question(key=msg.id, text=msg.content, links=tuple(links))


class ModelContextUpdate(BaseModel):
    """Параметры ui/update-model-context приложения MCP Apps: что приложение
    хочет сообщить модели к следующему вопросу — текст и структурные данные.
    Картинок и прочих блоков чат приложению не объявляет, валидация их
    отвергает."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: Sequence[mt.TextContent] = ()
    structured_content: Mapping[str, Any] = Field(
        default_factory=dict, alias="structuredContent"
    )

    def blocks(self) -> Iterator[PromptBlock]:
        """Блоки вопроса агенту только для модели: при воспроизведении
        вопрос показывается без них."""
        annotations = schema.Annotations(audience=[BlockAudience.MODEL.value])
        for block in self.content:
            yield schema.TextContentBlock(text=block.text, annotations=annotations)

        if not self.structured_content:
            return

        text = JsonBlock.pretty(self.structured_content)
        yield schema.TextContentBlock(text=text, annotations=annotations)


class PermissionKind(StrEnum):
    """Виды вариантов ответа на вопрос разрешения."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"


@dataclass(frozen=True)
class ThreadIdentity:
    """Кто и в каком треде говорит с агентом: тред, пользователь, его вход и
    профиль чата. Собирается из сессии chainlit (ThreadIdentities). session —
    живой вход пользователя вкладки: по нему регистрация сервера агента
    читает роли и секреты входа в момент вызова."""

    thread_id: str
    user_id: UUID
    login: str
    roles: frozenset[str]
    session: LiveSignIn
    profile: SelectedProfile

    def registration(self, server: str) -> McpRegistration:
        """Регистрация сервера server для агента или приложения этой
        вкладки: вызовы идут от имени её пользователя в её треде."""
        return McpRegistration(
            user_id=self.user_id,
            login=self.login,
            thread_id=self.thread_id,
            profile=self.profile.name,
            session=self.session,
            server=server,
            servers=frozenset(self.profile.config.mcp),
        )


class ThreadIdentities:
    """Сборка ThreadIdentity из сессии chainlit вкладки и выбранного профиля.
    Держат её в поле обработчики вкладки (TabAgent) и ретранслятор
    приложений MCP Apps."""

    def of(self, session: ChainlitSession, profile: SelectedProfile) -> ThreadIdentity:
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

        return ThreadIdentity(
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
    при правке вопроса; слой данных этот ключ от chainlit не принимает.
    Вместе с сессией пишется профиль чата треда (chat_profile, тот же ключ,
    что chainlit сохраняет на отключении вкладки): по нему удаление треда
    поднимает агента, даже если вкладка ещё не отключалась."""

    def __init__(self, threads: ThreadsTable) -> None:
        self._threads = threads

    async def session_of(self, thread_id: str) -> str | None:
        """Id сессии агента для загрузки из metadata треда; None — треда нет
        или сессия ему ещё не открыта, и первый вопрос откроет новую."""
        stored = await self._threads.get(UUID(thread_id))
        if stored is None:
            return None

        return self.in_meta(stored.meta)

    @staticmethod
    def in_meta(meta: Any) -> str | None:
        """Id сессии агента в metadata треда; None — его там нет."""
        return AgentSessionMeta._text_in(meta, ThreadMetaField.AGENT_SESSION)

    @staticmethod
    def profile_in(meta: Any) -> str | None:
        """Профиль чата треда в его metadata; None — его там нет."""
        return AgentSessionMeta._text_in(meta, ThreadMetaField.CHAT_PROFILE)

    @staticmethod
    def _text_in(meta: Any, key: str) -> str | None:
        if not isinstance(meta, Mapping):
            return None

        value: object = meta.get(key)
        if not isinstance(value, str):
            return None

        if not value:
            return None

        return value

    async def remember(self, thread_id: str, session_id: str, profile: str) -> None:
        """Сессия треда и профиль чата, под которым она открыта."""
        change = ThreadUpsert(
            id=UUID(thread_id),
            meta_set={
                ThreadMetaField.AGENT_SESSION: session_id,
                ThreadMetaField.CHAT_PROFILE: profile,
            },
        )
        await self._threads.upsert(change)


class TurnRenderer(SessionUpdates, CommandBoard, Protocol):
    """Приёмник потока сессии, который знает и о ходе вкладки: его начало,
    остановку, очистку ленты и конец воспроизведения истории; он же —
    вкладка для реестра команд (меню и окна команд). Реализует AgentFeed
    (feed.py).
    """

    @abstractmethod
    def stopping(self, session_id: str) -> None:
        """Ход останавливается: висящие вопросы разрешения и вопросы
        пользователю получат отмену."""

    @abstractmethod
    async def question_shown(
        self, anchor: WebsocketSession, live: Collection[str]
    ) -> bool:
        """Вкладка anchor подключилась (заново): висящий вопрос пользователю
        показывается в ней. live — сессии вкладок треда с подключённым
        сокетом: вопрос, показанный другой живой вкладке, остаётся там.
        False — вопроса нет либо он остался в другой вкладке."""

    @abstractmethod
    async def turn_started(
        self, question: Question, start: Coroutine[Any, Any, StartedTurn]
    ) -> StartedTurn:
        """Ход по вопросу начинается: start — ещё не начатое начало хода
        (сессия, `session/prompt`), его ждёт приёмник уже под замком области
        треда; итог — начатый ход с ключом, который агент дал вопросу.

        Ошибки:
        LockBusyError — тред уже ведёт другой держатель.
        AcpClientError — агент не принял вопрос.
        """

    @abstractmethod
    async def inserted(self, question: Question) -> None:
        """Вопрос из очереди сессии вставлен агентом: шаг вкладки с id
        вопроса снимается, сам вопрос рисует эхо агента."""

    @abstractmethod
    async def withdrawn(self, questions: Sequence[Question]) -> None:
        """Вопросы очереди сняты отменой хода до вставки: их шаги во вкладке
        снимаются, текст возвращается в поле ввода в порядке отправки."""

    @abstractmethod
    def connected(self) -> bool:
        """Сокет вкладки приёмника подключён прямо сейчас."""

    @abstractmethod
    async def cleared(self) -> None:
        """Лента вкладки очищается перед повторным воспроизведением истории.

        Ошибки:
        InternalServiceError — треда нет в слое данных.
        DataUnavailableError — тред не прочитан.
        """

    @abstractmethod
    async def loaded(self, session_id: str) -> None:
        """`session/resume` с воспроизведением ответил: история нарисована."""


class TurnRenderers(Protocol):
    """Фабрика приёмников потока для агентов вкладки; реализует AgentFeeds."""

    @abstractmethod
    async def create(self, identity: ThreadIdentity) -> TurnRenderer:
        """Приёмник для агента треда identity.

        Ошибки:
        McpClientError — серверы MCP профиля не открылись.
        """


class MuteUpdates(SessionUpdates):
    """Приёмник без ленты: короткоживущий процесс удаления сессии; вопросы
    пользователю он снимает."""

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        return

    async def elicitation(
        self, request: schema.CreateFormSessionElicitationRequest
    ) -> ElicitationReply:
        return schema.CancelElicitationResponse()

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


class TurnEnd:
    """Конец хода вкладки по потоку сессии: idle агента, после которого не
    ждёт вставки ни один вопрос, поставленный по ходу в очередь сессии.

    Вопрос очереди ждёт от отправки `session/prompt` до ответа и эха его
    `user_message` — в любом порядке — либо до отказа; idle до этого —
    граница между ходами агента, а не конец хода вкладки. Команда вкладки,
    которую агент пишет в историю по ходу, держит конец хода до ответа на
    запись; её эхо хода не начинает и idle не отменяет. Создаёт
    AskedQuestions на ход, вопросы очереди и команды отмечает ThreadAgent;
    снятые до вставки вопросы копятся в порядке отправки (withdrawn).
    """

    def __init__(self) -> None:
        self.settled: asyncio.Future[schema.IdleSessionStateUpdate]
        self.settled = asyncio.get_running_loop().create_future()
        self._sending: list[Question] = []
        self._withdrawn: list[Question] = []
        self._unechoed: set[str] = set()
        self._echoed: set[str] = set()
        self._recording: set[str] = set()
        self._recorded: set[str] = set()
        self._idle: schema.IdleSessionStateUpdate | None = None

    def recording(self, message_id: str) -> bool:
        """Команда вкладки с id записи message_id уходит в историю идущего
        хода; False — ход уже кончился."""
        if self.settled.done():
            return False

        self._recording.add(message_id)
        self._recorded.add(message_id)

        return True

    def recorded(self, message_id: str) -> None:
        """Агент ответил на запись команды message_id."""
        self._recording.discard(message_id)
        self._settle()

    def sent(self, question: Question) -> bool:
        """Вопрос уходит в очередь сессии идущего хода; False — ход уже
        кончился, вопрос начнёт свой."""
        if self.settled.done():
            return False

        self._sending.append(question)

        return True

    def answered(self, question: Question, message_id: str) -> None:
        """Агент вставил вопрос и дал ему message_id."""
        self._sending.remove(question)
        if message_id not in self._echoed:
            self._unechoed.add(message_id)

        self._settle()

    def refused(self, question: Question, withdrawn: bool) -> None:
        """Вопрос не вставлен; withdrawn — его сняла отмена хода, и текст
        вернётся в поле ввода."""
        self._sending.remove(question)
        if withdrawn:
            self._withdrawn.append(question)

        self._settle()

    def withdrawn(self) -> Sequence[Question]:
        """Снятые до вставки вопросы в порядке отправки."""
        return list(self._withdrawn)

    def update(self, update: SessionUpdate) -> None:
        if isinstance(update, schema.UserMessageUpdate):
            if update.message_id in self._recorded:
                return

            self._echoed.add(update.message_id)
            self._unechoed.discard(update.message_id)
            self._idle = None
            return

        if isinstance(update, schema.IdleSessionStateUpdate):
            self._idle = update
            self._settle()
            return

        if isinstance(
            update,
            schema.RunningSessionStateUpdate | schema.RequiresActionSessionStateUpdate,
        ):
            self._idle = None

    def _settle(self) -> None:
        if self.settled.done():
            return

        idle = self._idle
        if idle is None:
            return

        if self._sending:
            return

        if self._recording:
            return

        if self._unechoed:
            return

        self.settled.set_result(idle)


class AskedQuestions(SessionUpdates):
    """Приёмник-обёртка агента треда: запоминает вложения вопросов, которые
    агент показал вкладке (`user_message` воспроизведения и живое эхо), решает
    ожидание конца хода по idle, отдаёт команды агента реестру команд
    вкладки и передаёт поток дальше рендереру. По вложениям правка вопроса
    сохраняет их для модели."""

    def __init__(self, inner: SessionUpdates, menu: CommandMenu) -> None:
        self._inner = inner
        self._menu = menu
        self.links: dict[str, tuple[schema.ResourceContentBlock, ...]] = {}
        """messageId вопроса → его блоки resource_link."""
        self._ending: dict[str, TurnEnd] = {}
        """Конец хода по сессии; ждут одну сессию, его решает idle агента."""

    def ending(self, session_id: str) -> TurnEnd:
        """Конец хода сессии session_id. Ставится до `session/prompt`, чтобы
        idle не прошёл мимо; заменяет прежний."""
        ending = TurnEnd()
        self._ending.clear()
        self._ending[session_id] = ending

        return ending

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        if isinstance(update, schema.UserMessageUpdate):
            content = update.content
            if content is None:
                content = []

            links: list[schema.ResourceContentBlock] = []
            for block in content:
                if isinstance(block, schema.ResourceContentBlock):
                    links.append(block)

            self.links[update.message_id] = tuple(links)

        if isinstance(update, schema.AvailableCommandsUpdate):
            await self._menu.heard(update)

        ending = self._ending.get(session_id)
        if ending is not None:
            ending.update(update)
            if ending.settled.done():
                self._ending.pop(session_id)

        await self._inner.update(session_id, update)

    async def permission(
        self, session_id: str, title: str, options: Sequence[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        return await self._inner.permission(session_id, title, options)

    async def elicitation(
        self, request: schema.CreateFormSessionElicitationRequest
    ) -> ElicitationReply:
        return await self._inner.elicitation(request)

    async def turn_finished(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        await self._inner.turn_finished(session_id, idle)

    async def abandon(self, session_id: str) -> None:
        await self._inner.abandon(session_id)


@dataclass(frozen=True)
class AgentLaunch:
    """Заготовка запуска агента по текущему профилю чата: имя профиля (у
    службы — имя профиля агента), профиль модели и системный промпт для
    файла настроек своего процесса, сроки серверов MCP профиля. Собирает
    AgentLauncher.prepare до остановки прежнего процесса, а ProcessLinks — для
    короткого процесса удаления сессии."""

    profile: str
    model: ModelFile
    system_prompt: str
    mcp: McpFile | None


@dataclass(frozen=True)
class LaunchedAgent:
    """Запущенный агент вкладки и серверы, объявленные ему записями type
    "acp": serverId каждого — регистрация в McpRegistrations. Отдаёт
    AgentLauncher, держит ThreadAgent и возвращает запуску на снятие
    регистраций (AgentLauncher.retire)."""

    process: AgentLink
    servers: Sequence[schema.AcpMcpServer]


class AgentLinks(Protocol):
    """Как чат достаёт агента вкладки: свой процесс (ProcessLinks) или
    служба агента (ServiceLinks). Выбирает AgentLauncher по [agent.process]."""

    @abstractmethod
    async def link(
        self,
        thread_id: str,
        caller: McpCaller,
        launch: AgentLaunch,
        servers: Sequence[schema.AcpMcpServer],
        client: AgentClient,
    ) -> AgentLink:
        """Агент вкладки пользователя caller после рукопожатия.

        Ошибки:
        AgentProcessError — агент не запустился или не ответил.
        PostgresError — билет роли истории не получен.
        """

    @abstractmethod
    async def deleting(self, thread: StoredThread, profile: str) -> AgentLink:
        """Агент без серверов и ленты для `session/delete` сессии удаляемого
        треда с профилем чата profile — по текущему конфигу и строкам чата.

        Ошибки:
        AgentSettingsError — профиля нет в конфиге или он не годится агенту.
        AgentProcessError — агент не запустился или у треда нет владельца.
        DataUnavailableError — строка владельца не прочитана.
        PostgresError — билет роли истории не получен.
        """


class ProcessLinks(AgentLinks):
    """Реализация AgentLinks своим процессом агента: заготовка запуска → файл
    настроек в свежем приватном каталоге → процесс. kerberos-роль истории
    получает окружение libkrb5 ключом `env` файла. Приватный каталог
    удаляется при любом несостоявшемся запуске. Процесс удаления сессии
    собирается по профилю треда из текущих [profiles]. Создаёт AgentLauncher."""

    def __init__(  # noqa: PLR0913 — процесс собирается конфигом и входами чата
        self,
        config: AgentConfig,
        process: AgentCommandConfig,
        servers: McpServersConfig,
        profiles: ChatProfiles,
        relay: McpRelay,
        version: str,
    ) -> None:
        self._relay = relay
        self._version = version
        self._profiles = profiles
        self._specs = ProfileModelSpecs()
        self._files = SettingsFileBuilder(config, servers)
        self._workdirs = AgentWorkdirs(Path(process.private_dir))
        self._environment = HistoryEnvironment(config.history)
        program = process.program()
        self._command = AgentCommand(
            executable=program[0],
            args=(*program[1:], *process.args),
            start_timeout_sec=process.start_timeout_sec,
            request_timeout_sec=process.request_timeout_sec,
            stop_timeout_sec=process.stop_timeout_sec,
        )

    async def link(
        self,
        thread_id: str,
        caller: McpCaller,
        launch: AgentLaunch,
        servers: Sequence[schema.AcpMcpServer],
        client: AgentClient,
    ) -> AgentLink:
        return await self._started(thread_id, launch, servers, client)

    async def deleting(self, thread: StoredThread, profile: str) -> AgentLink:
        declared = self._profiles.all
        config = declared.get(profile)
        if config is None:
            msg = (
                f"deleting the agent session of thread {thread.id}: the chat "
                f"profile {profile!r} of the thread is not in [profiles] of the "
                f"config, expected one of {sorted(declared)}; the agent process "
                "needs a model profile to start"
            )
            raise AgentSettingsError(msg)

        model = self._specs.model(profile, config)
        launch = AgentLaunch(
            profile=profile, model=model, system_prompt=config.system_prompt, mcp=None
        )

        client = AgentClient(MuteUpdates())

        return await self._started(str(thread.id), launch, (), client)

    async def _started(
        self,
        thread_id: str,
        launch: AgentLaunch,
        servers: Sequence[schema.AcpMcpServer],
        client: AgentClient,
    ) -> AgentLink:
        env = await self._environment.prepare()
        document = self._files.build(launch.profile, launch.model, launch.mcp, env)
        workdir = self._workdirs.create(thread_id)
        try:
            workdir.write(document, launch.system_prompt)
        except OSError as exc:
            workdir.remove()
            msg = (
                f"thread {thread_id}: writing the agent settings into "
                f"{workdir.path} failed: {exc}"
            )
            failure = AgentFailure(
                kind=AgentFailureKind.START_FAILED,
                peer=self._command.peer(),
                action=LinkAction.STARTING,
                cause=f"its settings file cannot be written: {exc.strerror}",
            )
            raise AgentProcessError(msg, failure) from exc

        process = AgentProcess(
            self._command, workdir, client, self._relay, servers, self._version
        )
        await process.start()

        return process


class ServiceLinks(AgentLinks):
    """Реализация AgentLinks службой агента: соединение ACP от имени
    пользователя треда, профиль агента — имя профиля чата. Файла настроек
    нет: модели, промпт и историю держит служба. Удаление сессии входит от
    имени владельца треда: логин и роли — из его строки users, где их
    оставил последний вход. Создаёт AgentLauncher."""

    def __init__(
        self,
        service: AgentServiceConfig,
        users: UserRows,
        relay: McpRelay,
        version: str,
    ) -> None:
        self._service = service
        self._users = users
        self._relay = relay
        self._version = version

    async def link(
        self,
        thread_id: str,
        caller: McpCaller,
        launch: AgentLaunch,
        servers: Sequence[schema.AcpMcpServer],
        client: AgentClient,
    ) -> AgentLink:
        return await self._started(caller, launch.profile, servers, client)

    async def deleting(self, thread: StoredThread, profile: str) -> AgentLink:
        caller = await self._owner(thread)
        client = AgentClient(MuteUpdates())

        return await self._started(caller, profile, (), client)

    async def _owner(self, thread: StoredThread) -> McpCaller:
        owner = thread.user_id
        if owner is None:
            msg = (
                f"deleting the agent session of thread {thread.id}: the thread "
                "has no owner to sign in to the agent service as"
            )
            failure = AgentFailure(
                kind=AgentFailureKind.START_FAILED,
                peer=self._service.peer(),
                action="deleting the thread",
                cause="the thread has no owner to sign in as",
            )
            raise AgentProcessError(msg, failure)

        stored = await self._users.stored_by_id(owner)
        if stored is None:
            msg = (
                f"deleting the agent session of thread {thread.id}: its owner "
                f"{owner} has no users row to sign in to the agent service as"
            )
            failure = AgentFailure(
                kind=AgentFailureKind.START_FAILED,
                peer=self._service.peer(),
                action="deleting the thread",
                cause="the owner of the thread is not a known user",
            )
            raise AgentProcessError(msg, failure)

        user = stored.authenticated()

        return McpCaller(login=user.identifier, roles=user.roles)

    async def _started(
        self,
        caller: McpCaller,
        profile: str,
        servers: Sequence[schema.AcpMcpServer],
        client: AgentClient,
    ) -> AgentLink:
        service = AgentService(
            self._service,
            caller,
            profile,
            client,
            self._relay,
            servers,
            self._version,
        )
        await service.start()

        return service


class AgentLauncher:
    """Запуск агента под тред: заготовка из текущего профиля треда →
    регистрации серверов профиля и сервера chat → агент: свой процесс или
    служба (AgentLinks). Удалению треда даёт агента для `session/delete` по
    профилю из threads.meta и текущему конфигу.

    Создаётся провайдером процесса; зовут ThreadAgent и ThreadDeleter.
    Регистрации выдаёт и снимает реестр McpRegistrations: каждый запуск получает
    новые serverId, остановка процесса и несостоявшийся запуск их снимают.
    Запросы mcp/message агента отвечает общий ретранслятор (McpRelay).
    """

    def __init__(  # noqa: PLR0913 — запуск собирается конфигом и входами процесса
        self,
        config: AgentConfig,
        servers: McpServersConfig,
        profiles: ChatProfiles,
        users: UserRows,
        registrations: McpRegistrations,
        relay: McpRelay,
        version: str,
    ) -> None:
        self._specs = ProfileModelSpecs()
        self._registrations = registrations
        self._relay = relay
        self._files = SettingsFileBuilder(config, servers)
        self._links = self._chosen(config, servers, profiles, users, relay, version)

    @staticmethod
    def _chosen(  # noqa: PLR0913 — вид агента выбирается всеми входами запуска
        config: AgentConfig,
        servers: McpServersConfig,
        profiles: ChatProfiles,
        users: UserRows,
        relay: McpRelay,
        version: str,
    ) -> AgentLinks:
        process = config.process
        if isinstance(process, AgentServiceConfig):
            return ServiceLinks(process, users, relay, version)

        return ProcessLinks(config, process, servers, profiles, relay, version)

    def prepare(self, identity: ThreadIdentity) -> AgentLaunch:
        """Заготовка запуска по текущему профилю треда: то, что может
        отказать до остановки прежнего процесса.

        Ошибки:
        AgentSettingsError — профиль не годится агенту или называет сервер
            без секции [mcp.servers].
        """
        profile = identity.profile
        model = self._specs.model(profile.name, profile.config)
        mcp = self._files.mcp(profile.config.mcp)

        return AgentLaunch(
            profile=profile.name,
            model=model,
            system_prompt=profile.config.system_prompt,
            mcp=mcp,
        )

    async def launch(
        self, identity: ThreadIdentity, launch: AgentLaunch, client: AgentClient
    ) -> LaunchedAgent:
        """Агент под вкладку треда по заготовке: серверы профиля и сервер
        чата регистрируются от имени пользователя вкладки новыми serverId.

        Ошибки:
        AgentProcessError — агент не запустился или не ответил.
        PostgresError — билет роли истории не получен.
        """
        names = list(identity.profile.config.mcp)
        names.append(ChatTools.NAME)

        registration = identity.registration(ChatTools.NAME)
        servers = self._registrations.register(registration, names)
        caller = McpCaller(login=identity.login, roles=identity.roles)
        try:
            process = await self._links.link(
                identity.thread_id, caller, launch, servers, client
            )
        except Exception:
            for server in servers:
                self._registrations.revoke(server.server_id)

            raise

        return LaunchedAgent(process=process, servers=servers)

    def retire(self, launched: LaunchedAgent) -> None:
        """Снимает регистрации остановленного процесса: ретранслятор их больше
        не принимает, вызовы, открытые для вопросов, отменены."""
        self.stopped(launched)
        for server in launched.servers:
            self._registrations.revoke(server.server_id)

    def stopped(self, launched: LaunchedAgent) -> None:
        """Ход процесса остановлен: вызовы серверов прежней редакции, которые
        ретранслятор держит открытыми для вопросов пользователю, отменены."""
        server_ids: list[str] = []
        for server in launched.servers:
            server_ids.append(server.server_id)

        self._relay.stopped(server_ids)

    async def deleting(self, thread: StoredThread) -> AgentLink:
        """Агент для `session/delete` сессии удаляемого треда: профиль чата —
        из threads.meta, остальное — текущее состояние (AgentLinks.deleting).

        Ошибки:
        AgentSettingsError — в metadata треда нет профиля чата, профиля нет
            в конфиге или он не годится агенту.
        AgentProcessError — агент не запустился или у треда нет владельца.
        DataUnavailableError — строка владельца не прочитана.
        PostgresError — билет роли истории не получен.
        """
        profile = AgentSessionMeta.profile_in(thread.meta)
        if profile is None:
            msg = (
                f"deleting the agent session of thread {thread.id}: the thread "
                f"metadata names no chat profile ({ThreadMetaField.CHAT_PROFILE}) "
                "to reach the agent with"
            )
            raise AgentSettingsError(msg)

        return await self._links.deleting(thread, profile)


@dataclass(frozen=True)
class StartedTurn:
    """Начатый ход вкладки: сессия агента, ожидание её idle и ключ хода —
    messageId, который агент дал вопросу. Его собирает ThreadAgent._started,
    а приёмник TurnRenderer возвращает после захвата замка области."""

    session_id: str
    ending: TurnEnd
    key: str


class ThreadAgent:
    """Агент одной вкладки треда: процесс, сессия агента и текущий ход.

    Создаётся реестром ThreadAgents; живёт пока открыта вкладка, а процесс —
    пока вкладка подключена: отключённая вкладка его отпускает (release), и
    следующее дело вкладки запускает новый. Сессия
    открывается `session/new` на первом вопросе или `session/resume` с
    воспроизведением при возврате к треду; правка вопроса ветвит сессию
    `session/fork`; режим разрешений открытой сессии процесс выставляет по
    ACP (AgentLink: свой процесс или служба). Ход кончается idle агента
    (TurnEnd) либо
    выходом процесса. Вышедший процесс заменяется новым перед следующим ходом:
    запуск, смена процесса и начало хода идут под одним замком вкладки.

    Сессия открыта у процесса только на время хода, воспроизведения и
    перезапуска: потом процесс её закрывает и отпускает замок истории, а
    следующий ход снова открывает её `session/resume` без ленты. Так вопрос
    любой вкладки треда на любом инстансе ведёт её собственный процесс, а
    ходы разводит замок области треда в приёмнике (TurnRenderer).
    """

    def __init__(
        self,
        identity: ThreadIdentity,
        launcher: AgentLauncher,
        meta: AgentSessionMeta,
        renderer: TurnRenderer,
        menus: CommandMenus,
    ) -> None:
        self._identity = identity
        self._launcher = launcher
        self._meta = meta
        self._renderer = renderer
        registration = identity.registration(ChatTools.NAME)
        self._menu = menus.menu(registration, renderer)
        self._questions = AskedQuestions(renderer, self._menu)
        self._launched: LaunchedAgent | None = None
        self._session_id: str | None = None
        self._open = False
        """Сессия _session_id открыта у процесса и держит замок истории."""
        self._unclaimed = False
        """Сессию _session_id вкладка открыла до первого вопроса, и в
        threads.meta её ещё нет: первый вопрос её туда пишет, закрытие без
        вопроса удаляет её у агента."""
        self._active: str | None = None
        self._current: StartedTurn | None = None
        """Ход вкладки от начала до отпущенного замка области."""
        self._closed = asyncio.Event()
        """Хода вкладки нет: следующий вопрос начнёт свой."""
        self._closed.set()
        self._gate = asyncio.Lock()
        self._restart_pending = False
        self._detached = False
        """Вкладка отключилась: процесс останавливается, как только нет хода."""
        self._redraw = False
        """Правка другой вкладки сменила ветку треда, пока эта была отключена:
        первым делом вернувшейся вкладки её лента рисуется заново."""
        self._contexts: dict[str, Sequence[PromptBlock]] = {}

    @property
    def thread_id(self) -> str:
        return self._identity.thread_id

    @property
    def user_id(self) -> UUID:
        return self._identity.user_id

    @property
    def session_id(self) -> str | None:
        """Id сессии агента; None — сессия ещё не открыта."""
        return self._session_id

    @property
    def process(self) -> AgentLink:
        if self._launched is None:
            msg = f"agent of thread {self.thread_id}: the process is not started"
            raise InternalServiceError(internal_detail=msg, user_detail=None)

        return self._launched.process

    def turn_active(self) -> bool:
        return self._active is not None

    async def start(self) -> None:
        """Процесс агента и меню команд вкладки. Треду без сессии агента
        вкладка сразу открывает новую: агент объявляет команды только своей
        сессии, и меню получает их с открытием вкладки."""
        async with self._gate:
            await self._launch(self._launcher.prepare(self._identity))
            await self._opened_ahead()

        await self._menu.start(self.process.records_commands())

    async def _opened_ahead(self) -> None:
        """Новая сессия треду, у которого в threads.meta её нет; агент её не
        открыл — процесс закрывается, и вкладка получает отказ.

        Ошибки:
        AcpClientError — агент отказал в `session/new`.
        """
        known = await self._meta.session_of(self.thread_id)
        if known is not None:
            return

        try:
            opened = await self.process.open_session()
        except AcpClientError:
            await self._close_process()
            raise

        self._session_id = opened.session_id
        self._open = True
        self._unclaimed = True
        logger.info(
            "thread %s: agent session %s opened with the tab",
            self.thread_id,
            opened.session_id,
        )

    async def routed(self, question: Question, command: str | None) -> Question | None:
        """Вопрос вкладки с выбранной в меню командой command — владельцу
        команды: вопрос агенту (с текстом `/<имя> <аргументы>`, если это
        вызов команды) либо None — команду исполнила вкладка, она записана
        в историю агента и шаг вопроса уступил её эху. Вопрос-правка идёт
        агенту как есть.

        Ошибки:
        CommandError — команду, которую исполняет вкладка, агент в историю
            не пишет; без записи сервер недоступен.
        AgentRequestError — агент отказал в записи команды.
        AgentProcessError — агент ушёл, не записав команду.
        """
        if question.edited:
            return question

        routed = await self._menu.routed(question.text, command)
        if isinstance(routed, str):
            return question.model_copy(update={"text": routed})

        await self._record(question, routed)

        return None

    async def _record(self, question: Question, executed: ExecutedCommand) -> None:
        """Команда, которую исполнила вкладка, — записью истории агента с
        новым id: во время хода вкладки — в его сессию (агент запишет её на
        границе шага), между ходами — в сессию треда, открытую на время
        записи, как для хода. Шаг вопроса снимается после записи — эхо уже
        нарисовано под id записи."""
        message_id = str(uuid4())
        while True:
            async with self._gate:
                current = self._current
                if current is None:
                    await self._record_between_turns(question, executed, message_id)
                    break

                recording = current.ending.recording(message_id)

            if recording:
                await self._record_in_turn(current, executed, message_id)
                break

            await self._closed.wait()

        await self._renderer.inserted(question)

    async def _record_in_turn(
        self, turn: StartedTurn, executed: ExecutedCommand, message_id: str
    ) -> None:
        request = self._record_request(turn.session_id, executed, message_id)
        try:
            await self.process.record(request)
        finally:
            turn.ending.recorded(message_id)

        logger.info(
            "thread %s: command /%s is recorded in the running turn as %s",
            self.thread_id,
            executed.call.name,
            message_id,
        )

    async def _record_between_turns(
        self, question: Question, executed: ExecutedCommand, message_id: str
    ) -> None:
        """Запись под замком вкладки: вышедший процесс заменяется, сессия
        треда открывается без ленты и закрывается после записи."""
        await self._revive()
        if self._exited():
            self._restart_pending = True

        if self._restart_pending:
            await self._restart()

        session_id = await self._session_for(question)
        request = self._record_request(session_id, executed, message_id)
        try:
            await self.process.record(request)
        finally:
            await self._close_session(self.process)

        logger.info(
            "thread %s: command /%s is recorded in session %s as %s",
            self.thread_id,
            executed.call.name,
            session_id,
            message_id,
        )

    @staticmethod
    def _record_request(
        session_id: str, executed: ExecutedCommand, message_id: str
    ) -> SessionRecordRequest:
        return SessionRecordRequest(
            session_id=session_id,
            message_id=message_id,
            command=executed.call.name,
            text=executed.call.text(),
            outcome=executed.outcome,
        )

    async def _restart(self) -> None:
        """Замена процесса под замком вкладки перед ходом: файл настроек
        собирается заново по профилю, прежний процесс закрывается, новый
        запускается и открывает сессию треда без воспроизведения ленты
        (`session/resume`). Несостоявшаяся замена повторится перед следующим
        ходом.

        Ошибки:
        AgentSettingsError — заготовка запуска не собрана.
        AgentProcessError, AgentRequestError — процесс не запустился или
            отказал в сессии.
        """
        self._restart_pending = True
        launch = self._launcher.prepare(self._identity)
        await self._close_process()
        await self._launch(launch)
        try:
            await self._resume(replay=False)
        finally:
            await self._close_session(self.process)

        self._restart_pending = False
        logger.info(
            "thread %s: agent restarted: %s, session=%s",
            self.thread_id,
            self.process.label(),
            self._session_id,
        )

    async def _revive(self) -> None:
        """Процесс, отпущенный отключённой вкладкой, запускается заново перед
        делом вкладки; сессия откроется ходом без воспроизведения ленты."""
        if self._launched is not None:
            return

        if not self._detached:
            return

        self._detached = False
        await self._launch(self._launcher.prepare(self._identity))

    async def _launch(self, launch: AgentLaunch) -> None:
        """Новый процесс по заготовке: файл настроек в свежем приватном
        каталоге, новые регистрации серверов, рукопожатие."""
        self._launched = await self._launcher.launch(
            self._identity, launch, AgentClient(self._questions)
        )

    async def resume(self) -> bool:
        """Сохранённая сессия треда с воспроизведением беседы; False — у
        агента нет такой сессии, первый вопрос откроет новую.

        Ошибки:
        AgentRequestError — агент отказал в `session/resume` не по причине
            неизвестной сессии.
        """
        async with self._gate:
            await self._revive()
            try:
                return await self._resume(replay=True)
            finally:
                await self._close_session(self.process)

    async def _resume(self, replay: bool) -> bool:
        """Сессия треда в процессе вкладки: с воспроизведением беседы — при
        возврате к треду (id из threads.meta), без него — открытая сессия в
        новом процессе после перезапуска.
        False — сессии нет: открытой нет или агент её не знает, и первый
        вопрос откроет новую."""
        known = self._session_id
        if replay:
            self._redraw = False
            known = await self._meta.session_of(self.thread_id)

        if known is None:
            return False

        try:
            await self.process.resume_session(known, replay)
        except AgentRequestError as exc:
            if exc.code != AcpErrorCode.RESOURCE_NOT_FOUND:
                raise

            logger.info(
                "thread %s: no agent session %s yet: %s", self.thread_id, known, exc
            )
            self._session_id = None

            return False

        self._session_id = known
        self._open = True
        if not replay:
            return True

        await self._renderer.loaded(known)
        logger.info("thread %s: agent session %s resumed", self.thread_id, known)

        return True

    async def reload(self) -> None:
        """Вкладка заново рисует беседу треда из истории агента: прежняя
        сессия процесса закрывается, лента вкладки очищается, сессия треда из
        threads.meta воспроизводится. Так вкладка показывает отказ правки и
        ветку, которую сделала другая вкладка.

        Ошибки:
        AgentRequestError — агент отказал в `session/close` или
            `session/resume`.
        InternalServiceError, DataUnavailableError — тред не прочитан.
        """
        async with self._gate:
            await self._revive()
            await self._redrawn()

    async def rewound(self) -> None:
        """Правка другой вкладки сменила ветку треда: подключённая вкладка
        перерисовывается сразу, отключённая — первым делом, когда вернётся.
        Ради ленты без зрителя процесс не поднимается и замка вкладки
        рендерер треда не ждёт.

        Ошибки: как у reload.
        """
        if not self._renderer.connected():
            self._redraw = True
            logger.info(
                "thread %s: the tab is disconnected, its feed is redrawn on return",
                self.thread_id,
            )
            return

        await self.reload()

    async def _redrawn(self) -> None:
        """Лента вкладки заново из сессии треда в threads.meta: под замком
        вкладки, процесс уже запущен."""
        await self._close_session(self.process)
        self._session_id = None
        await self._renderer.cleared()
        try:
            await self._resume(replay=True)
        finally:
            await self._close_session(self.process)

    async def recover(self) -> None:
        """Вкладка просит агента заново после сбоя: агент, который вышел или
        не поднялся, заменяется новым, лента вкладки рисуется заново из его
        истории.

        Ошибки:
        AgentProcessError, AgentRequestError — агент снова не поднялся или
            отказал в `session/resume`.
        AgentSettingsError, DataUnavailableError — слой чата не собран.
        InternalServiceError — тред не прочитан.
        """
        async with self._gate:
            self._detached = False
            if self._launched is None or self._exited():
                launch = self._launcher.prepare(self._identity)
                await self._close_process()
                await self._launch(launch)
                self._restart_pending = False

            await self._close_session(self.process)
            self._session_id = None
            await self._renderer.cleared()
            try:
                await self._resume(replay=True)
            finally:
                await self._close_session(self.process)

    def edited(self, question: Question) -> Question:
        """Вопрос-правка с вложениями правленого вопроса из беседы, которую
        вкладка видела у агента.

        Ошибки:
        QuestionEditError — вопроса вкладка не видела: его задали в другой
            вкладке после её открытия.
        """
        links = self._questions.links.get(question.key)
        if links is None:
            msg = (
                f"edit of question {question.key} in thread {self.thread_id}: the "
                "question is not in the conversation loaded in this tab"
            )
            raise QuestionEditError(msg, EditRefusal.NOT_LOADED)

        return question.model_copy(update={"links": links, "edited": True})

    async def ask(self, question: Question) -> None:
        """Ход по вопросу: новая сессия на первом, ветвь на правке; ход
        кончается idle агента. Вопрос во время идущего хода вкладки уходит в
        очередь той же сессии агента и ход продолжает; правку во время хода
        отказывает замок области.
        Кнопка Stop снимает задачу обработчика chainlit: ход всё равно
        дожидается idle агента после отмены и отмечает его в ленте.
        Вышедший процесс (упал сам или посреди хода) перед ходом заменяется
        новым; несостоявшаяся замена повторяется перед следующим ходом.

        Ошибки:
        AgentRequestError — агент отказал в методе сессии.
        AgentProcessError — процесс умер по ходу.
        LockBusyError — тред уже ведёт другой держатель.
        """
        while True:
            async with self._gate:
                current = self._current
                if current is None:
                    turn = await self._begin(question)
                    break

                # правку идущий ход не ждёт: замок области отказывает ей сразу
                if question.edited:
                    turn = await self._begin(question)
                    break

                queued = current.ending.sent(question)

            if queued:
                await self._enqueued(question, current)
                return

            await self._closed.wait()

        try:
            await self._carried(turn)
        finally:
            self._current = None
            self._closed.set()
            self._menu.revisit()

    async def _begin(self, question: Question) -> StartedTurn:
        """Начало хода под замком вкладки: вышедший процесс заменяется,
        вопрос уходит агенту под замком области треда."""
        await self._revive()
        if self._exited():
            logger.warning(
                "thread %s: the agent process exited, a new one starts",
                self.thread_id,
            )
            self._restart_pending = True

        if self._restart_pending:
            await self._restart()

        if self._redraw:
            await self._redrawn()

        turn = await self._renderer.turn_started(question, self._started(question))
        self._active = turn.session_id
        self._current = turn
        self._closed.clear()
        logger.info(
            "thread %s: turn %s starts in session %s",
            self.thread_id,
            turn.key,
            turn.session_id,
        )

        return turn

    async def _carried(self, turn: StartedTurn) -> None:
        """Ход до idle агента, после которого не ждёт ни один вопрос очереди."""
        session_id = turn.session_id
        try:
            idle = await self._idle(turn)
        except asyncio.CancelledError:
            if not turn.ending.settled.done():
                self._renderer.stopping(session_id)
                self._stopped()
                await self.process.cancel(session_id)

            idle = await self._idle(turn)
            await self._finish(turn, idle)
            raise
        except Exception:
            self._active = None
            try:
                await self._released()
            finally:
                await self._renderer.abandon(session_id)

            raise

        await self._finish(turn, idle)

    async def _enqueued(self, question: Question, turn: StartedTurn) -> None:
        """Вопрос в очередь сессии идущего хода: агент отвечает на
        `session/prompt`, когда вставил вопрос своим ходом. Обработчик ждёт
        конца хода вкладки — индикатор хода горит, пока идёт и он; Stop
        снимает задачу обработчика, но исход вопроса всё равно учитывается.

        Ошибки:
        AcpClientError — агент отказал в вопросе не отменой хода.
        """
        logger.info(
            "thread %s: question %s goes to the queue of session %s",
            self.thread_id,
            question.key,
            turn.session_id,
        )
        blocks = self._blocks(question)
        request = asyncio.ensure_future(self.process.prompt(turn.session_id, blocks))
        try:
            await asyncio.wait((request,))
        except asyncio.CancelledError:
            await asyncio.wait((request,))
            await self._queued_outcome(question, turn, request)
            raise

        await self._queued_outcome(question, turn, request)
        await self._closed.wait()

    async def _queued_outcome(
        self, question: Question, turn: StartedTurn, request: asyncio.Future[str]
    ) -> None:
        """Исход вопроса очереди: вставлен — шаг вкладки уступает эху агента;
        снят отменой хода — вернётся в поле ввода в конце хода.

        Ошибки:
        AcpClientError — агент отказал в вопросе не отменой хода.
        """
        try:
            key = request.result()
        except AgentRequestError as exc:
            withdrawn = exc.code == AcpErrorCode.REQUEST_CANCELLED
            turn.ending.refused(question, withdrawn)
            if not withdrawn:
                raise

            logger.info(
                "thread %s: queued question %s was withdrawn by the stop: %s",
                self.thread_id,
                question.key,
                exc,
            )
            return
        except AcpClientError:
            turn.ending.refused(question, False)
            raise

        turn.ending.answered(question, key)
        logger.info(
            "thread %s: queued question %s was inserted as %s",
            self.thread_id,
            question.key,
            key,
        )
        await self._renderer.inserted(question)

    def _exited(self) -> bool:
        """Процесс вкладки был запущен и больше не отвечает: вышел (упал сам или
        посреди хода) либо закрыл свой вывод."""
        launched = self._launched
        if launched is None:
            return False

        return not launched.process.alive()

    async def _started(self, question: Question) -> StartedTurn:
        """Начало хода, которое приёмник исполняет под замком области треда:
        сессия хода (новая, открытая или ветка при правке), ожидание её idle
        и вопрос агенту; ключ хода — messageId, который агент дал вопросу."""
        session_id = await self._session_for(question)
        ending = self._questions.ending(session_id)
        try:
            key = await self.process.prompt(session_id, self._blocks(question))
        except AcpClientError:
            await self._close_session(self.process)
            raise

        return StartedTurn(session_id=session_id, ending=ending, key=key)

    async def _idle(self, turn: StartedTurn) -> schema.IdleSessionStateUpdate:
        """idle хода либо выход процесса, который idle уже не пришлёт.

        Ошибки:
        AgentProcessError — процесс вышел посреди хода.
        """
        settled = turn.ending.settled
        exited = asyncio.ensure_future(self.process.ended())
        try:
            await asyncio.wait((settled, exited), return_when=asyncio.FIRST_COMPLETED)
        finally:
            exited.cancel()

        if settled.done():
            return settled.result()

        failure = self.process.lost(LinkAction.TURN)
        msg = (
            f"thread {self.thread_id}: {self.process.label()} left during the turn "
            f"of session {turn.session_id} ({failure.cause}): "
            f"{self.process.log_tail()}"
        )
        raise AgentProcessError(msg, failure)

    async def _finish(
        self, turn: StartedTurn, idle: schema.IdleSessionStateUpdate
    ) -> None:
        self._active = None
        session_id = turn.session_id
        # замок истории отпущен раньше замка области: вопрос другой вкладки,
        # пришедший сразу за концом хода, застаёт сессию свободной
        try:
            await self._released()
        finally:
            try:
                await self._renderer.withdrawn(turn.ending.withdrawn())
            finally:
                await self._renderer.turn_finished(session_id, idle)

        logger.info(
            "thread %s: turn of session %s finished with %s",
            self.thread_id,
            session_id,
            idle.stop_reason,
        )

    async def _released(self) -> None:
        """Сессия хода закрыта у процесса под замком вкладки; процесс уже
        закрыт вместе с вкладкой — закрывать нечего, а вкладка, отключившаяся
        по ходу, отпускает его целиком."""
        async with self._gate:
            launched = self._launched
            if launched is None:
                return

            if self._detached:
                await self._close_process()
                return

            await self._close_session(launched.process)

    async def question_shown(
        self, anchor: WebsocketSession, live: Collection[str]
    ) -> bool:
        """Висящий вопрос пользователю этого агента — во вкладку anchor,
        если его не показывает другая живая вкладка (live); False — не
        показан."""
        return await self._renderer.question_shown(anchor, live)

    async def stop(self) -> bool:
        """Отмена идущего хода; False — хода нет."""
        active = self._active
        if active is None:
            return False

        self._renderer.stopping(active)
        self._stopped()
        await self.process.cancel(active)
        logger.info("thread %s: cancel sent for session %s", self.thread_id, active)

        return True

    def _stopped(self) -> None:
        launched = self._launched
        if launched is None:
            return

        self._launcher.stopped(launched)

    async def release(self) -> None:
        """Вкладка отключилась: процесс без хода останавливается сразу, с
        ходом — по его концу. Вернувшаяся вкладка запустит новый первым же
        делом (_revive); закрытый агент (close) остаётся закрытым."""
        async with self._gate:
            if self._launched is None:
                return

            self._detached = True
            if self._active is not None:
                return

            await self._close_process()

    async def close(self) -> None:
        """Закрытие сессии у агента (замок отпущен), остановка процесса и
        потоков меню команд."""
        try:
            async with self._gate:
                await self._close_process()
        finally:
            await self._menu.close()

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

    async def _close_session(self, process: AgentLink) -> None:
        """Открытая сессия закрыта у процесса: замок истории отпущен."""
        session_id = self._session_id
        if session_id is None:
            return

        if not self._open:
            return

        self._open = False
        unclaimed = self._unclaimed
        if unclaimed:
            self._session_id = None
            self._unclaimed = False

        if not process.alive():
            return

        await process.close_session(session_id)
        logger.info("thread %s: agent session %s closed", self.thread_id, session_id)
        if not unclaimed:
            return

        await process.delete_session(session_id)
        logger.info(
            "thread %s: agent session %s deleted, the tab asked nothing in it",
            self.thread_id,
            session_id,
        )

    async def _session_for(self, question: Question) -> str:
        """Сессия хода: ветвь до вопроса при его правке, сессия треда — уже
        открытая или открытая заново без ленты; агент её не знает — новая."""
        current = self._session_id
        if current is None:
            return await self._created()

        if question.edited:
            return await self._branch(question, current)

        if self._open:
            await self._claimed(current)
            return current

        if await self._resume(replay=False):
            return current

        return await self._created()

    async def _claimed(self, session_id: str) -> None:
        """Сессия, открытая вкладкой заранее, пишется в threads.meta с первым
        вопросом."""
        if not self._unclaimed:
            return

        profile = self._identity.profile.name
        await self._meta.remember(self.thread_id, session_id, profile)
        self._unclaimed = False

    async def _created(self) -> str:
        """Новая сессия треда: её id запоминается в threads.meta."""
        opened = await self.process.open_session()
        created = opened.session_id
        await self._meta.remember(self.thread_id, created, self._identity.profile.name)
        self._session_id = created
        self._open = True
        logger.info("thread %s: agent session %s created", self.thread_id, created)

        return created

    async def _branch(self, question: Question, current: str) -> str:
        """Ветка беседы до правленого вопроса: `session/fork` с срезом по его
        messageId, затем `session/delete` прежней сессии — у треда всегда одна
        сессия. Прежнюю не удалить (её держит процесс другой вкладки или
        отказало хранилище) — ветка снимается, threads.meta не меняется,
        правка отказана; агент без объявленного среза правку отказывает.

        Ошибки:
        AcpClientError — агент не объявил срез ветки или прежняя сессия не
            удалена.
        AgentRequestError — агент отказал в `session/fork`.
        """
        if not self.process.forks_by_message():
            msg = (
                f"edit of question {question.key} in thread {self.thread_id}: the "
                f"agent does not declare {MetaKey.UP_TO_MESSAGE_ID.value!r} in "
                "capabilities.session.fork._meta"
            )
            raise QuestionEditError(msg, EditRefusal.NO_BRANCHING)

        branch = await self.process.fork(current, question.key)
        forked = branch.session_id
        try:
            await self.process.delete_session(current)
        except AgentRequestError as refused:
            logger.warning(
                "thread %s: question %s not edited, session %s not deleted: %s",
                self.thread_id,
                question.key,
                current,
                refused,
            )
            try:
                await self.process.delete_session(forked)
            except AgentRequestError as dropped:
                logger.error(
                    "thread %s: branch %s not removed either: %s",
                    self.thread_id,
                    forked,
                    dropped,
                )
                msg = (
                    f"edit of question {question.key} in thread {self.thread_id}: "
                    f"session {current} not deleted ({refused}), branch {forked} "
                    f"not removed either: {dropped}"
                )
                raise QuestionEditError(msg, EditRefusal.NOT_EDITED) from dropped

            msg = (
                f"edit of question {question.key} in thread {self.thread_id}: "
                f"session {current} not deleted: {refused}"
            )
            raise QuestionEditError(msg, EditRefusal.NOT_EDITED) from refused

        await self._meta.remember(self.thread_id, forked, self._identity.profile.name)
        self._session_id = forked
        self._open = True
        logger.info(
            "thread %s: question %s edited, session %s forked into %s and deleted",
            self.thread_id,
            question.key,
            current,
            forked,
        )

        return forked

    def app_context(self, call_id: str, blocks: Sequence[PromptBlock]) -> None:
        """Контекст приложения MCP Apps вызова call_id уйдёт модели со
        следующим вопросом; новый контекст того же приложения заменяет
        прежний."""
        self._contexts[call_id] = blocks

    def _blocks(self, question: Question) -> list[PromptBlock]:
        """Текст вопроса, ссылки на его вложения и ждущие контексты
        приложений: они уходят один раз."""
        blocks: list[PromptBlock] = [
            schema.TextContentBlock(text=question.text),
            *question.links,
        ]
        for context in self._contexts.values():
            blocks.extend(context)

        self._contexts.clear()

        return blocks


class ThreadAgentsIndex(ThreadRewinds):
    """Агенты всех вкладок инстанса по тредам: удаление треда закрывает их
    первыми, чтобы сессия агента не осталась под замком этого процесса;
    срез истории правкой из другой вкладки перерисовывает их ленты.
    Реализация ThreadRewinds рендерера треда."""

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

    async def rewound(self, thread_id: str, session_id: str) -> None:
        """Каждый агент треда не на ветке session_id перечитывает её в свою
        вкладку (отключённая — когда вернётся); правящая вкладка уже на
        ветке, хвост она обрезала сама.
        Отказ одной вкладки не оставляет без перерисовки остальные.

        Ошибки:
        InternalServiceError — какие-то вкладки не перерисованы.
        """
        failures: list[str] = []
        for agent in self.of_thread(thread_id):
            if agent.session_id == session_id:
                continue

            try:
                await agent.rewound()
            except (AcpClientError, InternalServiceError, DataLayerError) as exc:
                logger.error(
                    "thread %s: a tab did not reload branch %s: %s",
                    thread_id,
                    session_id,
                    exc,
                )
                failures.append(str(exc))

        if failures:
            msg = (
                f"thread {thread_id}: {len(failures)} tab(s) did not show the "
                f"branch {session_id} after an edit: " + "; ".join(failures)
            )
            raise InternalServiceError(
                internal_detail=msg,
                user_detail="The edited conversation is not shown; reload the chat",
            )

    async def close_thread(self, thread_id: str) -> int:
        """Закрывает агентов треда во всех вкладках инстанса; итог — сколько."""
        agents = self.of_thread(thread_id)
        for agent in agents:
            self.forget(agent)
            await agent.close()

        return len(agents)


class TabAgentsStep(StrEnum):
    """Шаг над всеми агентами вкладки: начало текста его отказа."""

    CLOSE = "closing"
    RELEASE = "releasing"


class ThreadAgents:
    """Агенты вкладки chainlit по тредам: один процесс на открытый тред,
    пока вкладка подключена (release на отключении); закрывается с
    DI-контейнером сессии."""

    def __init__(
        self,
        launcher: AgentLauncher,
        meta: AgentSessionMeta,
        index: ThreadAgentsIndex,
        renderers: TurnRenderers,
        menus: CommandMenus,
    ) -> None:
        self._launcher = launcher
        self._meta = meta
        self._index = index
        self._renderers = renderers
        self._menus = menus
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

            renderer = await self._renderers.create(identity)
            agent = ThreadAgent(
                identity, self._launcher, self._meta, renderer, self._menus
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
        """Закрывает агентов всех тредов вкладки разом; отказ одного не
        мешает закрыть остальных и поднимается после всех.

        Ошибки:
        AgentProcessError — агенты каких-то тредов не закрылись: тред и причина
            каждого.
        """
        agents = list(self._agents.values())
        self._agents.clear()

        closing: list[Coroutine[Any, Any, None]] = []
        for agent in agents:
            self._index.forget(agent)
            closing.append(agent.close())

        await self._settled(TabAgentsStep.CLOSE, agents, closing)

    async def release(self) -> None:
        """Вкладка отключилась: агенты её тредов разом отпускают процессы
        (ThreadAgent.release) и остаются в реестре до конца сессии.

        Ошибки:
        AgentProcessError — процессы каких-то тредов не остановились: тред и
            причина каждого.
        """
        agents = list(self._agents.values())

        releasing: list[Coroutine[Any, Any, None]] = []
        for agent in agents:
            releasing.append(agent.release())

        await self._settled(TabAgentsStep.RELEASE, agents, releasing)

    async def _settled(
        self,
        step: TabAgentsStep,
        agents: Sequence[ThreadAgent],
        steps: Sequence[Coroutine[Any, Any, None]],
    ) -> None:
        """Ждёт шагов всех агентов; отказы собираются в одну ошибку после всех."""
        outcomes = await asyncio.gather(*steps, return_exceptions=True)

        failures: list[AcpClientError] = []
        reasons: list[str] = []
        failure: AgentFailure | None = None
        for agent, outcome in zip(agents, outcomes, strict=True):
            if outcome is None:
                continue

            if not isinstance(outcome, AcpClientError):
                raise outcome

            failures.append(outcome)
            reasons.append(f"thread {agent.thread_id}: {outcome}")
            if failure is None:
                failure = AgentFailure(
                    kind=AgentFailureKind.CONNECTION_LOST,
                    peer=f"agent of thread {agent.thread_id}",
                    action=step.value,
                    cause=str(outcome),
                )

        if failure is not None:
            msg = (
                f"{step.value} the agents of the tab: {len(failures)} of "
                f"{len(agents)} failed: {'; '.join(reasons)}"
            )
            raise AgentProcessError(msg, failure) from failures[0]


class ThreadDeleter(ThreadDeletionListener):
    """Удаление сессии агента вместе с тредом: агенты вкладок этого инстанса
    закрываются (отпускают замок), затем короткий агент по текущему
    состоянию (AgentLauncher.deleting) делает `session/delete`.

    Реализация ThreadDeletionListener слоя данных; создаётся провайдером
    процесса и ставится на PostgresDataLayer в bootstrap. Сессию, которую
    держит другой инстанс, агент удалить не даст — тред остаётся.
    """

    def __init__(self, launcher: AgentLauncher, index: ThreadAgentsIndex) -> None:
        self._launcher = launcher
        self._index = index

    async def thread_deleted(self, thread: StoredThread) -> None:
        """Ошибки:
        AgentRequestError — агент отказал в `session/delete`.
        AgentProcessError — агент не запустился или у треда нет владельца.
        AgentSettingsError — профиль треда не назван или исчез из конфига.
        DataUnavailableError — строка владельца не прочитана.
        """
        thread_id = str(thread.id)
        closed = await self._index.close_thread(thread_id)
        if closed:
            logger.info(
                "thread %s: %d tab agent(s) closed before deletion", thread_id, closed
            )

        session_id = AgentSessionMeta.in_meta(thread.meta)
        if session_id is None:
            return

        process = await self._launcher.deleting(thread)
        try:
            await process.delete_session(session_id)
            logger.info("thread %s: agent session %s deleted", thread_id, session_id)
        finally:
            await process.close()
