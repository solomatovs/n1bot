"""Агент треда в chainlit: процесс на вкладку, id сессии в threads.meta, ход
и его остановка, удаление сессии с тредом и минимальный вывод потока в ленту.

Ошибки:
AcpClientError и наследники — процесс или ответ агента (client.py).
AgentSettingsError — профиль чата не годится агенту (settings.py).
McpTokenError, SessionServersError — серверы MCP сессии (tokens.py).
DataUnavailableError — таблицы чата недоступны.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from acp import resource_link_block, text_block
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    PromptResponse,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
)
from acp.schema import ContentChunk as AcpContentChunk

import chainlit as cl
from boba.chainlit.acp.client import (
    AgentClient,
    AgentCommand,
    AgentProcess,
    AgentRequestError,
    JsonRpcCode,
    PromptBlock,
    SessionUpdate,
    SessionUpdates,
)
from boba.chainlit.acp.config import AgentConfig
from boba.chainlit.acp.settings import (
    AgentSettingsLayers,
    AgentSettingsTable,
    AgentWorkdirs,
    ChatAgentSettings,
    HistoryEnvironment,
    SettingsFileBuilder,
)
from boba.chainlit.acp.tokens import SessionServer, SessionServers
from boba.chainlit.chat.turn import Question
from boba.chainlit.data.data_layer import ThreadDeletionListener
from boba.chainlit.domain.fields import ThreadMetaField
from boba.chainlit.infra.session import ChainlitSession
from boba.chat.profiles import SelectedProfile
from boba.chat.threads import ThreadUpsert
from boba.identity.errors import InternalServiceError
from boba.runtime.threads import ThreadsTable

__all__ = [
    "AgentLauncher",
    "AgentSessionMeta",
    "FeedRenderer",
    "ThreadAgent",
    "ThreadAgents",
    "ThreadAgentsIndex",
    "ThreadDeleter",
    "ThreadIdentity",
]

logger = logging.getLogger(__name__)


class PermissionKind(StrEnum):
    """Виды вариантов ответа на вопрос разрешения."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"
    REJECT_ALWAYS = "reject_always"


class PermissionOutcome(StrEnum):
    """Исходы ответа на вопрос разрешения."""

    SELECTED = "selected"
    CANCELLED = "cancelled"


class StepName(StrEnum):
    """Имена и виды шагов ленты минимального рендерера."""

    THINKING = "thinking"
    TOOL_TYPE = "tool"
    THOUGHT_TYPE = "undefined"
    USER_TYPE = "user_message"


class ToolStatus(StrEnum):
    """Статусы вызова инструмента в уведомлениях."""

    FAILED = "failed"


class StopReasonName(StrEnum):
    """Исходы хода, которые рендерер различает в ленте."""

    END_TURN = "end_turn"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ThreadIdentity:
    """Кто и в каком треде говорит с агентом: тред, пользователь, его вход и
    профиль чата. Собирается из сессии chainlit в обработчике."""

    thread_id: str
    user_id: UUID
    login: str
    roles: frozenset[str]
    jwt: str
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
            jwt=session.token,
            profile=profile,
        )


class AgentSessionMeta:
    """Соответствие тред → sessionId агента в threads.meta: его читают
    открытие треда и удаление, пишет первый ход."""

    def __init__(self, threads: ThreadsTable) -> None:
        self._threads = threads

    async def session_of(self, thread_id: str) -> str:
        """Id сессии агента для загрузки: из metadata треда, а без записи —
        id самого треда: так названы сессии мигрированных тредов, и так
        их находит `session/load`; неизвестную сессию агент отвергнет сам."""
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


@dataclass
class LiveFeed:
    """Открытые узлы ленты одной сессии: ответ, рассуждение, шаги инструментов."""

    answer: cl.Message | None = None
    answer_id: str | None = None
    thought: cl.Step | None = None
    thought_id: str | None = None
    steps: dict[str, cl.Step] = field(default_factory=dict)


class FeedRenderer(SessionUpdates):
    """Минимальный вывод потока сессии в ленту chainlit: куски ответа — в
    сообщение, рассуждения — в шаг, вызовы инструментов — шагами с title,
    вопросы разрешения — первым вариантом allow либо отменой при остановке.

    Реализация SessionUpdates; рендерер ленты W11 встаёт на её место.
    """

    def __init__(self) -> None:
        self._feeds: dict[str, LiveFeed] = {}
        self._stopping: set[str] = set()

    def stopping(self, session_id: str) -> None:
        """Ход останавливается: висящие вопросы разрешения получат отмену."""
        self._stopping.add(session_id)

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        feed = self._feeds.setdefault(session_id, LiveFeed())
        if isinstance(update, AgentMessageChunk):
            await self._answer_chunk(feed, update)
            return

        if isinstance(update, AgentThoughtChunk):
            await self._thought_chunk(feed, update)
            return

        if isinstance(update, ToolCallStart):
            await self._tool_call(feed, update)
            return

        if isinstance(update, ToolCallProgress):
            await self._tool_update(feed, update)
            return

        if isinstance(update, UserMessageChunk):
            await self._user_chunk(update)
            return

        logger.debug(
            "session %s: update %s is not rendered", session_id, type(update).__name__
        )

    async def permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: Sequence[PermissionOption],
    ) -> RequestPermissionResponse:
        if session_id in self._stopping:
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome=PermissionOutcome.CANCELLED.value)
            )

        chosen = self._allowing(options)
        logger.info(
            "session %s: permission for %s answered with %s",
            session_id,
            tool_call.tool_call_id,
            chosen.option_id,
        )

        return RequestPermissionResponse(
            outcome=AllowedOutcome(
                outcome=PermissionOutcome.SELECTED.value, option_id=chosen.option_id
            )
        )

    async def turn_finished(self, session_id: str, stop_reason: str) -> None:
        self._stopping.discard(session_id)
        feed = self._feeds.pop(session_id, None)
        if feed is None:
            feed = LiveFeed()

        await self._close_feed(feed)

        if stop_reason == StopReasonName.END_TURN:
            return

        note = f"_the turn ended early: {stop_reason}_"
        if stop_reason == StopReasonName.CANCELLED:
            note = "_stopped by the user_"

        await cl.Message(content=note).send()

    async def abandon(self, session_id: str) -> None:
        """Ход сорвался: открытые узлы закрываются, пометки не ставится."""
        self._stopping.discard(session_id)
        feed = self._feeds.pop(session_id, None)
        if feed is None:
            return

        await self._close_feed(feed)

    @staticmethod
    def _allowing(options: Sequence[PermissionOption]) -> PermissionOption:
        """Первый вариант «разрешить»; без него — первый из предложенных."""
        for option in options:
            if option.kind == PermissionKind.ALLOW_ONCE:
                return option

        for option in options:
            if option.kind == PermissionKind.ALLOW_ALWAYS:
                return option

        return options[0]

    async def _close_feed(self, feed: LiveFeed) -> None:
        if feed.thought is not None:
            await feed.thought.send()

        if feed.answer is not None:
            await feed.answer.send()

    async def _answer_chunk(self, feed: LiveFeed, update: AgentMessageChunk) -> None:
        text = self._text(update)
        if text is None:
            return

        if feed.answer is not None and feed.answer_id != update.message_id:
            await feed.answer.send()
            feed.answer = None

        if feed.answer is None:
            feed.answer = cl.Message(content="")
            feed.answer_id = update.message_id

        await feed.answer.stream_token(text)

    async def _thought_chunk(self, feed: LiveFeed, update: AgentThoughtChunk) -> None:
        text = self._text(update)
        if text is None:
            return

        if feed.thought is not None and feed.thought_id != update.message_id:
            await feed.thought.send()
            feed.thought = None

        if feed.thought is None:
            feed.thought = cl.Step(
                name=StepName.THINKING.value, type=StepName.THOUGHT_TYPE.value
            )
            feed.thought_id = update.message_id

        await feed.thought.stream_token(text)

    async def _tool_call(self, feed: LiveFeed, update: ToolCallStart) -> None:
        step = cl.Step(name=update.title, type=StepName.TOOL_TYPE.value)
        if update.raw_input is not None:
            step.input = json.dumps(update.raw_input, ensure_ascii=False)

        feed.steps[update.tool_call_id] = step
        await step.send()

    async def _tool_update(self, feed: LiveFeed, update: ToolCallProgress) -> None:
        step = feed.steps.get(update.tool_call_id)
        if step is None:
            title = update.title
            if title is None:
                title = update.tool_call_id

            step = cl.Step(name=title, type=StepName.TOOL_TYPE.value)
            feed.steps[update.tool_call_id] = step
            await step.send()

        texts = list(self._tool_texts(update))
        if texts:
            step.output = "\n".join(texts)

        if update.status == ToolStatus.FAILED:
            step.is_error = True

        await step.update()

    async def _user_chunk(self, update: UserMessageChunk) -> None:
        text = self._text(update)
        if text is None:
            return

        await cl.Message(content=text, type=StepName.USER_TYPE.value).send()

    @staticmethod
    def _text(update: AcpContentChunk) -> str | None:
        """Текст блока куска; картинки и ресурсы минимальный рендерер не показывает."""
        content = update.content
        if isinstance(content, TextContentBlock):
            return content.text

        return None

    @staticmethod
    def _tool_texts(update: ToolCallProgress) -> Sequence[str]:
        found: list[str] = []
        if update.content is None:
            return found

        for item in update.content:
            if item.type != "content":
                continue

            block = item.content
            if isinstance(block, TextContentBlock):
                found.append(block.text)

        return found


class AskedQuestions(SessionUpdates):
    """Приёмник-обёртка: запоминает id вопросов, воспроизведённых агентом
    (`user_message_chunk.messageId`), и передаёт поток дальше рендереру. По
    этим id агент треда узнаёт правку вопроса после возврата к треду."""

    def __init__(self, inner: SessionUpdates) -> None:
        self._inner = inner
        self.ids: list[str] = []

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        if isinstance(update, UserMessageChunk) and update.message_id is not None:
            if update.message_id not in self.ids:
                self.ids.append(update.message_id)

        await self._inner.update(session_id, update)

    async def permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: Sequence[PermissionOption],
    ) -> RequestPermissionResponse:
        return await self._inner.permission(session_id, tool_call, options)

    async def turn_finished(self, session_id: str, stop_reason: str) -> None:
        await self._inner.turn_finished(session_id, stop_reason)

    async def abandon(self, session_id: str) -> None:
        await self._inner.abandon(session_id)


class AgentLauncher:
    """Запуск процесса агента под тред: слой чата из Postgres → файл настроек
    в приватном каталоге → процесс → рукопожатие; серверы MCP сессии с
    заголовками собираются здесь же.

    Создаётся провайдером процесса; зовут ThreadAgent и ThreadDeleter.
    """

    def __init__(
        self,
        config: AgentConfig,
        layers: AgentSettingsLayers,
        servers: SessionServers,
        version: str,
    ) -> None:
        self._config = config
        self._layers = layers
        self._servers = servers
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

    async def launch(
        self, identity: ThreadIdentity, client: AgentClient
    ) -> AgentProcess:
        """Процесс под вкладку треда: слой чата, хук запечатывания с JWT входа.

        Ошибки:
        AgentSettingsError — профиль не годится агенту.
        SessionServersError — сервер профиля не объявлен или не того транспорта.
        AgentProcessError — процесс не запустился.
        DataUnavailableError — слой настроек недоступен.
        """
        chat = await self._layers.chat_layer(
            UUID(identity.thread_id),
            identity.user_id,
            identity.profile.name,
            identity.profile.config,
        )
        hooked = self._servers.hooked(identity.profile.config.mcp)

        return await self.launch_settings(
            identity.thread_id, chat, identity.jwt, hooked, client
        )

    async def launch_settings(
        self,
        thread_id: str,
        chat: ChatAgentSettings,
        user_jwt: str,
        hooked: Sequence[str],
        client: AgentClient,
    ) -> AgentProcess:
        """Процесс по готовому слою чата; без hooked секция hooks пуста;
        kerberos-роль истории получает окружение libkrb5.

        Ошибки:
        AgentProcessError — процесс не запустился.
        KerberosError — билет истории по keytab не получен.
        """
        document = self._files.build(chat, user_jwt, hooked)
        workdir = self._workdirs.create(thread_id)
        workdir.write(document, chat.system_prompt)
        command = self._command.with_env(await self._environment.prepare())

        process = AgentProcess(command, workdir, client, self._version)
        await process.start()

        return process

    async def servers(self, identity: ThreadIdentity) -> list[SessionServer]:
        """Ошибки:
        McpTokenError — токен boba-mcp не получен.
        SessionServersError — сервер профиля не годится агенту.
        """
        return await self._servers.of(
            identity.profile.config.mcp, identity.login, identity.roles, identity.jwt
        )


class ThreadAgent:
    """Агент одной вкладки треда: процесс, сессия агента и текущий ход.

    Создаётся реестром ThreadAgents; живёт пока открыта вкладка. Сессия
    открывается `session/new` на первом вопросе или `session/load` при
    возврате к треду; правка вопроса ветвит сессию `session/fork`.
    """

    def __init__(
        self,
        identity: ThreadIdentity,
        launcher: AgentLauncher,
        meta: AgentSessionMeta,
        renderer: FeedRenderer,
    ) -> None:
        self._identity = identity
        self._launcher = launcher
        self._meta = meta
        self._renderer = renderer
        self._questions = AskedQuestions(renderer)
        self._process: AgentProcess | None = None
        self._servers: list[SessionServer] = []
        self._session_id: str | None = None
        self._asked: list[str] = []
        self._active: str | None = None

    @property
    def thread_id(self) -> str:
        return self._identity.thread_id

    @property
    def session_id(self) -> str | None:
        """Id сессии агента; None — сессия ещё не открыта."""
        return self._session_id

    @property
    def process(self) -> AgentProcess:
        if self._process is None:
            msg = f"agent of thread {self.thread_id}: the process is not started"
            raise InternalServiceError(internal_detail=msg, user_detail=None)

        return self._process

    def turn_active(self) -> bool:
        return self._active is not None

    async def start(self) -> None:
        """Процесс и серверы сессии; сессия открывается позже."""
        self._process = await self._launcher.launch(
            self._identity, AgentClient(self._questions)
        )
        self._servers = await self._launcher.servers(self._identity)

    async def resume(self) -> bool:
        """Загрузка сохранённой сессии треда с воспроизведением беседы;
        False — у агента нет такой сессии, первый вопрос откроет новую.

        Ошибки:
        AgentRequestError — агент отказал в `session/load` не по причине
            неизвестной сессии.
        """
        known = await self._meta.session_of(self.thread_id)
        try:
            await self.process.load_session(known, self._servers)
        except AgentRequestError as exc:
            if exc.code != JsonRpcCode.RESOURCE_NOT_FOUND:
                raise

            logger.info(
                "thread %s: no agent session %s yet: %s", self.thread_id, known, exc
            )

            return False

        self._session_id = known
        self._asked = list(self._questions.ids)
        await self._meta.remember(self.thread_id, known)
        logger.info(
            "thread %s: agent session %s loaded, %d question(s) replayed",
            self.thread_id,
            known,
            len(self._asked),
        )

        return True

    async def ask(self, question: Question) -> str:
        """Ход по вопросу: новая сессия на первом, ветвь на правке; итог —
        stopReason ответа. Кнопка Stop снимает задачу обработчика chainlit:
        ход всё равно дожидается ответа агента на отмену и отмечает его в ленте.

        Ошибки:
        AgentRequestError — агент отказал в методе сессии.
        AgentProcessError — процесс умер по ходу.
        """
        session_id = await self._session_for(question)
        self._asked.append(question.key)
        self._active = session_id
        logger.info(
            "thread %s: turn %s starts in session %s",
            self.thread_id,
            question.key,
            session_id,
        )
        turn = asyncio.create_task(
            self.process.prompt(session_id, self._blocks(question), question.key)
        )
        try:
            response = await asyncio.shield(turn)
        except asyncio.CancelledError:
            response = await self._cancelled_turn(session_id, turn)
            await self._finish(session_id, question, response)
            raise
        except Exception:
            self._active = None
            await self._renderer.abandon(session_id)
            raise

        await self._finish(session_id, question, response)

        return response.stop_reason

    async def _cancelled_turn(
        self, session_id: str, turn: asyncio.Task[PromptResponse]
    ) -> PromptResponse:
        """Задача обработчика снята: агент получает отмену, ход ждёт его ответа."""
        if not turn.done():
            self._renderer.stopping(session_id)
            await self.process.cancel(session_id)

        return await turn

    async def _finish(
        self, session_id: str, question: Question, response: PromptResponse
    ) -> None:
        self._active = None
        await self._renderer.turn_finished(session_id, response.stop_reason)
        logger.info(
            "thread %s: turn %s finished with %s",
            self.thread_id,
            question.key,
            response.stop_reason,
        )

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
        process = self._process
        if process is None:
            return

        try:
            await self._close_session(process)
        finally:
            await process.close()

    async def _close_session(self, process: AgentProcess) -> None:
        session_id = self._session_id
        if session_id is None:
            return

        if not process.alive():
            return

        await process.close_session(session_id)
        logger.info("thread %s: agent session %s closed", self.thread_id, session_id)

    async def _session_for(self, question: Question) -> str:
        """Сессия хода: открытая, ветвь при правке вопроса, иначе новая."""
        current = self._session_id
        if current is None:
            created = await self.process.new_session(self._servers)
            await self._meta.remember(self.thread_id, created)
            self._session_id = created
            logger.info("thread %s: agent session %s created", self.thread_id, created)

            return created

        if question.key not in self._asked:
            return current

        forked = await self.process.fork_session(current, self._servers, question.key)
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

    @staticmethod
    def _blocks(question: Question) -> list[PromptBlock]:
        """Текст вопроса и ссылки на вложения; вложение без адреса агенту не
        передаётся."""
        blocks: list[PromptBlock] = [text_block(question.text)]
        for element in question.elements:
            if element.url is None:
                continue

            blocks.append(
                resource_link_block(element.name, element.url, mime_type=element.mime)
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
        self, launcher: AgentLauncher, meta: AgentSessionMeta, index: ThreadAgentsIndex
    ) -> None:
        self._launcher = launcher
        self._meta = meta
        self._index = index
        self._agents: dict[str, ThreadAgent] = {}
        self._starting = asyncio.Lock()

    async def of(self, identity: ThreadIdentity) -> ThreadAgent:
        """Агент треда; при первом обращении процесс запускается. Старт вкладки
        и первый вопрос приходят почти разом — второй ждёт первого под замком."""
        async with self._starting:
            known = self._agents.get(identity.thread_id)
            if known is not None:
                return known

            agent = ThreadAgent(identity, self._launcher, self._meta, FeedRenderer())
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
            thread_id, chat, "", (), AgentClient(FeedRenderer())
        )
        try:
            await process.delete_session(session_id)
            logger.info("thread %s: agent session %s deleted", thread_id, session_id)
        finally:
            await process.close()
