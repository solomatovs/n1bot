"""Вход процесса агента: команда `boba acp --settings <файл>`, самопроверка
собранного бинарника `boba self-check` (boba.cli.package_data) и публикация
схемы файла настроек `boba settings-schema` (JSON Schema слоя на stdout).

Разбирает аргументы, собирает действующие настройки (Settings), на старте
проверяет профили моделей (ModelCatalog), хранилище истории и скиллы и отдаёт
входу ACP (boba.cli.acp_server) сборку сессий: на каждую `session/new|resume|fork`
— агент ядра над инструментами, которые собрал вход; на занятую другим
процессом сессию — наблюдатель; список, удаление и копия сессий (ветвление)
идут в хранилище истории; смена профиля модели в сессии берёт модель из
каталога. Стандартный вывод с самого начала отдан протоколу: дескриптор 1
перенаправлен в стандартный поток ошибок, и всё, что пишет туда кто-то ещё
(лог, чужие библиотеки), уходит в stderr. Конец ввода закрывает сессии и
сбрасывает журналы; `SIGTERM` и `SIGINT` прерывают ходы причиной SHUTDOWN, выход 0.

Ошибки:
SystemExit — настройки, профиль модели, хранилище или скиллы не годятся
    для запуска либо самопроверка не нашла данных пакетов в бинарнике; текст
    называет причину, код выхода 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import signal
import sys
from asyncio.streams import FlowControlMixin
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import BinaryIO, ClassVar

from acp.experimental.v2 import schema

from boba.agent.agent import Agent, AgentParts
from boba.agent.attachments import EnvironmentSnapshot
from boba.agent.events import EventSink
from boba.agent.history import (
    HistoryError,
    HistoryStore,
    MemoryHistoryStore,
    SessionHeldError,
    SessionId,
    SessionNotFoundError,
)
from boba.agent.ids import IdMint, SystemClock
from boba.agent.loader import HistoryLoader, SessionDigests
from boba.agent.mcp import ToolHints
from boba.agent.permissions import PermissionModeError
from boba.agent.profile import ModelProfile, ProfileError
from boba.agent.records import ChainRecord
from boba.agent.session import SessionError
from boba.agent.settings import (
    MemoryHistorySettings,
    PostgresHistorySettings,
    SettingsSchema,
)
from boba.agent.skills import Skill, SkillsDir, SkillsError
from boba.agent.template import AgentTemplate
from boba.agent.tools import McpServerSet, ToolsError, ToolServer
from boba.cancellation import ProcessStop, StopAction, StopReason
from boba.cli.acp_server import (
    AcpAgent,
    ObservedSession,
    OpenedSession,
    SessionAssembly,
    SessionBusyError,
    SessionOpenError,
    SessionPage,
    SessionRefusedError,
    SessionSummary,
)
from boba.cli.package_data import PackageDataCheck, PackageDataError
from boba.cli.settings import (
    Arguments,
    EffectiveSettings,
    EnvName,
    PostgresBuilder,
    ProfileBuilder,
    Settings,
    SettingsError,
    SettingsSource,
)
from boba.cli.threads import LoopThread, SessionThreads, ThreadedHistoryStore
from boba.db.postgres import AsyncPostgresPool
from boba.history.jsonl import JsonlHistoryStore
from boba.history.postgres import PostgresHistoryStore
from boba.llm.chat import ChatModel, LlmError
from boba.llm.http.ollama import MANIFEST as OLLAMA
from boba.llm.http.openai import MANIFEST as OPENAI
from boba.llm.onnx.chat import MANIFEST as ONNX
from boba.llm.providers import LlmProviders, LlmProvidersError, LlmProviderTypes
from boba.transport.acp.protocol import AgentSide, BatchTransport

logger = logging.getLogger(__name__)

__all__ = [
    "AgentAssembly",
    "AgentProcess",
    "ArgumentParser",
    "Command",
    "Installed",
    "Invocation",
    "ModelCatalog",
    "cli",
    "main",
]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class Command(StrEnum):
    """Команды `boba`."""

    ACP = "acp"
    SELF_CHECK = "self-check"
    SETTINGS_SCHEMA = "settings-schema"

    @property
    def serves(self) -> bool:
        """Команда поднимает процесс ACP; остальные печатают и выходят."""
        return self is Command.ACP


@dataclass(frozen=True)
class Invocation:
    """Разобранная командная строка: команда и параметры запуска; у
    `self-check` и `settings-schema` параметров нет, они остаются пустыми."""

    command: Command
    arguments: Arguments


class Installed:
    """Версия процесса из метаданных установленного дистрибутива: источник
    один — `pyproject.toml`, который правит `build/scripts/version.py`."""

    DISTRIBUTION: ClassVar[str] = "boba-cli"

    def version(self) -> str:
        """Ошибки:
        SystemExit — дистрибутив не установлен с метаданными.
        """
        try:
            return version(self.DISTRIBUTION)
        except PackageNotFoundError as exc:
            msg = (
                f"The distribution '{self.DISTRIBUTION}' is not installed with its "
                "metadata, so the agent cannot tell its version. Install the package "
                "(pip install -e packages/apps/boba-cli) instead of a bare .pth file."
            )
            raise SystemExit(msg) from exc


class ArgumentParser:
    """Командная строка `boba acp …`, `boba self-check` или
    `boba settings-schema` → Invocation (план, раздел 5.21). Секретов в
    аргументах нет: ключи и строки подключения живут в файле `--settings`."""

    PROG: ClassVar[str] = "boba"

    def __init__(self) -> None:
        self._parser = argparse.ArgumentParser(
            prog=self.PROG,
            description="Boba agent process: an Agent Client Protocol server on stdio.",
        )
        commands = self._parser.add_subparsers(dest="command", required=True)
        acp = commands.add_parser(
            Command.ACP.value,
            help="serve the Agent Client Protocol over stdin/stdout",
            description=(
                "Serve the Agent Client Protocol: JSON-RPC frames on stdin and "
                "stdout, logs on stderr. Settings come from settings.json layers "
                "and the arguments below."
            ),
        )
        self._define(acp)
        commands.add_parser(
            Command.SELF_CHECK.value,
            help="check that the package data of this build is inside, then exit",
            description=(
                "Check that every package data file named by the build manifest "
                "is reachable inside this binary. Reads nothing outside it, "
                "writes one line to stdout; exit 1 names the missing files."
            ),
        )
        commands.add_parser(
            Command.SETTINGS_SCHEMA.value,
            help="print the JSON Schema of a settings.json layer, then exit",
            description=(
                "Print the JSON Schema (draft 2020-12) of one settings.json layer "
                "to stdout: the contract of the file a launcher writes for this "
                "agent. Reads nothing."
            ),
        )

    def parse(self, argv: Sequence[str]) -> Invocation:
        space = self._parser.parse_args(list(argv))
        command = Command(space.command)
        if not command.serves:
            return Invocation(command=command, arguments=Arguments())

        sources: list[SettingsSource] = []
        for name in str(space.setting_sources).split(","):
            cleaned = name.strip()
            if not cleaned:
                continue

            try:
                sources.append(SettingsSource(cleaned))
            except ValueError:
                known = [s.value for s in SettingsSource]
                self._parser.error(
                    f"--setting-sources: unknown source {cleaned!r}, expected {known}"
                )

        arguments = Arguments(
            model=space.model,
            system_prompt=space.system_prompt,
            system_prompt_file=space.system_prompt_file,
            append_system_prompt=space.append_system_prompt,
            append_system_prompt_file=space.append_system_prompt_file,
            skills_dir=space.skills_dir,
            max_turns=space.max_turns,
            settings=tuple(space.settings),
            setting_sources=tuple(sources),
            policy=space.policy,
            history_root=space.history_root,
            record_requests=bool(space.record_requests),
        )

        return Invocation(command=command, arguments=arguments)

    @staticmethod
    def _define(parser: argparse.ArgumentParser) -> None:
        add = parser.add_argument
        add(
            "--settings",
            action="append",
            default=[],
            metavar="FILE_OR_JSON",
            help=(
                "settings.json file (mode 0600 when it holds secrets) or inline "
                "JSON layer without secrets; repeatable"
            ),
        )
        add("--model", help="name of the model profile from settings 'models'")
        add("--system-prompt", help="system prompt text")
        add("--system-prompt-file", help="file with the system prompt")
        add("--append-system-prompt", help="text appended to the system prompt")
        add("--append-system-prompt-file", help="file appended to the system prompt")
        add("--skills-dir", help="directory with <skill>/SKILL.md")
        add("--max-turns", type=int, help="model requests per turn")
        add(
            "--setting-sources",
            default=",".join(s.value for s in SettingsSource),
            help="file layers to read: user,project,local",
        )
        add("--policy", metavar="FILE", help="administrative settings layer")
        add("--history-root", help="directory of JSONL journals")
        add(
            "--record-requests",
            action="store_true",
            help="write an api-request record for every model request",
        )


class Environment:
    """Снимок окружения для модели: переменные процесса, прочитанные один
    раз, и рабочий каталог сессии."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def snapshot(self, cwd: Path) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            working_directory=str(cwd),
            platform=sys.platform,
            shell=self._env.get(EnvName.SHELL.value, ""),
            os_version=platform.platform(),
        )


class ModelCatalog:
    """Профили `models.<имя>` файла настроек: профиль ядра для каждого
    собирается на старте. Модель локального провайдера (веса в памяти)
    одна на процесс и хранится здесь; модель удалённого провайдера живёт
    клиентом http в цикле своей сессии, поэтому её даёт SessionModels
    сессии. Создаёт вход процесса; сборка берёт отсюда профили и модели
    сессий.
    """

    def __init__(
        self,
        settings: EffectiveSettings,
        env: Mapping[str, str],
        providers: LlmProviders,
    ) -> None:
        """Ошибки:
        SettingsError — профиль не описывает годного провайдера.
        """
        self._settings = settings
        self._providers = providers
        builder = ProfileBuilder(env)
        self._profiles: dict[str, ModelProfile] = {}
        for name, model in settings.models.items():
            self._profiles[name] = builder.profile(name, model)

        self._models: dict[str, ChatModel] = {}

    def names(self) -> Sequence[str]:
        return list(self._profiles)

    def has(self, name: str) -> bool:
        return name in self._profiles

    def profile(self, name: str) -> ModelProfile:
        """Ошибки:
        SettingsError — профиля с таким именем нет.
        """
        found = self._profiles.get(name)
        if found is None:
            known = sorted(self._profiles)
            msg = (
                f"The model profile '{name}' is not defined. Known profiles: {known}. "
                "Add it under 'models' in settings.json or pick another."
            )
            raise SettingsError(msg)

        return found

    def local(self, name: str) -> bool:
        """Провайдер профиля локальный: модель одна на процесс."""
        return not self._settings.models[name].provider.kind.remote

    def model(self, name: str) -> ChatModel:
        """Модель профиля на провайдерах процесса: локальная — общая всем
        сессиям, удалённая — для проверки на старте.

        Ошибки:
        SettingsError — профиля нет, либо его провайдер не собрался: в этой
            сборке нет его рантайма или веса модели не загрузились.
        """
        ready = self._models.get(name)
        if ready is not None:
            return ready

        model = self.built(name, self._providers)
        self._models[name] = model

        return model

    def built(self, name: str, providers: LlmProviders) -> ChatModel:
        """Модель профиля name на провайдерах providers.

        Ошибки:
        SettingsError — профиля нет либо его провайдер не собрался.
        """
        profile = self.profile(name)
        try:
            return providers.chat(profile.chat)
        except (LlmError, LlmProvidersError) as exc:
            kind = self._settings.models[name].provider.kind.value
            msg = (
                f"The model profile '{name}' (provider kind '{kind}') cannot be "
                f"used: {exc}. If this build was made without the runtime of that "
                "provider, pick another profile or use a build that includes it."
            )
            raise SettingsError(msg) from exc


class SessionModels:
    """Модели одной сессии: клиенты удалённых провайдеров создаются в цикле
    её потока и закрываются вместе с ней; локальные модели берутся общими у
    каталога процесса. Создаёт сборка на каждую открываемую сессию, держит
    AssembledSession."""

    def __init__(self, catalog: ModelCatalog, types: LlmProviderTypes) -> None:
        self._catalog = catalog
        self._providers = LlmProviders(types)
        self._models: dict[str, ChatModel] = {}

    def model(self, name: str) -> ChatModel:
        """Ошибки:
        SettingsError — профиля нет либо его провайдер не собрался.
        """
        if self._catalog.local(name):
            return self._catalog.model(name)

        ready = self._models.get(name)
        if ready is not None:
            return ready

        model = self._catalog.built(name, self._providers)
        self._models[name] = model

        return model

    async def close(self) -> None:
        await self._providers.aclose()


class AssembledSession(OpenedSession):
    """Реализация OpenedSession: агент, его модели и действующий профиль из
    каталога; смена профиля меняет модель и системный промпт сессии."""

    def __init__(
        self,
        agent: Agent,
        catalog: ModelCatalog,
        models: SessionModels,
        settings: EffectiveSettings,
        model: str,
    ) -> None:
        self._agent = agent
        self._catalog = catalog
        self._models = models
        self._settings = settings
        self._model = model

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def model(self) -> str:
        return self._model

    async def set_model(self, name: str) -> None:
        if not self._catalog.has(name):
            known = list(self._catalog.names())
            msg = f"the model profile '{name}' is not defined; known: {known}"
            raise SessionRefusedError(msg)

        previous = list(self._agent.system_prompt)
        try:
            profile = self._catalog.profile(name)
            model = self._models.model(name)
            self._agent.set_system_prompt(self._settings.system_prompt_of(name))
            self._agent.set_model(profile, model)
        except SessionError as exc:
            self._agent.set_system_prompt(previous)
            raise SessionBusyError(str(exc)) from exc
        except (SettingsError, ProfileError, HistoryError) as exc:
            self._agent.set_system_prompt(previous)
            msg = f"the model profile '{name}' cannot be used in this session: {exc}"
            raise SessionOpenError(msg) from exc

        self._model = name

    async def close(self) -> None:
        try:
            await self._agent.close()
        finally:
            await self._models.close()


class AssembledObserver(ObservedSession):
    """Реализация ObservedSession: сессия с записями в хранилище, открытая
    без замка, и инструменты, над которыми `claim` поднимает агента, когда
    замок свободен. Беседу наблюдателя воспроизведение читает из хранилища
    потоком; сам наблюдатель её не держит."""

    def __init__(  # noqa: PLR0913 — наблюдатель хранит всё для позднего открытия
        self,
        assembly: AgentAssembly,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        servers: McpServerSet,
        sink: EventSink,
        questions: bool,
    ) -> None:
        self._assembly = assembly
        self._session_id = session_id
        self._cwd = cwd
        self._tools = tools
        self._servers = servers
        self._sink = sink
        self._questions = questions

    def tool_hints(self, name: str) -> ToolHints | None:
        """Пометки инструмента по карточке; None — такого инструмента нет."""
        if self._tools is None:
            return None

        for card in self._tools.tools():
            if card.name == name:
                return card.hints

        return None

    async def claim(self) -> OpenedSession:
        return await self._assembly.open(
            self._session_id,
            self._cwd,
            self._tools,
            self._servers,
            self._sink,
            resume=True,
            questions=self._questions,
        )


class AgentAssembly(SessionAssembly):
    """Реализация SessionAssembly из действующих настроек процесса.

    Каталог профилей, хранилище и скиллы проверены на старте и общие для
    всех сессий; сессия открывается на профиле `model` настроек и может
    переключиться на любой другой из каталога. На сессию собираются снимок
    окружения с `cwd` сессии и агент ядра над инструментами от входа.
    Собственный инструмент «вопрос пользователю» предлагается, только если
    клиент объявил формы `elicitation.form`. С хранилищем `memory`
    возобновление сессии даёт пустую историю: возобновлять нечего, сессия
    открывается заново под тем же идентификатором, а ветвить нечего. В
    списке сессий рабочий каталог берётся из снимка окружения в записях, а у
    сессии без него — каталог самого процесса.
    """

    PAGE_SIZE: ClassVar[int] = 50

    def __init__(  # noqa: PLR0913 — сборка получает все проверенные части разом
        self,
        settings: EffectiveSettings,
        catalog: ModelCatalog,
        types: LlmProviderTypes,
        store: HistoryStore,
        skills: Sequence[Skill],
        environment: Environment,
        cwd: Path,
        version: str,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._types = types
        self._store = store
        self._skills = list(skills)
        self._environment = environment
        self._cwd = cwd
        self._version = version
        self._stateless = isinstance(settings.history, MemoryHistorySettings)
        self._loader = HistoryLoader(
            store, AgentTemplate(), IdMint(), SystemClock(), version
        )
        self._digests = SessionDigests(store)

    async def open(  # noqa: PLR0913 — сессия собирается всеми частями запроса
        self,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        servers: McpServerSet,
        sink: EventSink,
        resume: bool,
        questions: bool,
    ) -> OpenedSession:
        """Агент ядра над инструментами сессии на профиле `model` настроек со
        своими моделями сессии; с хранилищем `memory` возобновление
        становится открытием заново.

        Ошибки:
        SessionHeldError — сессию держит другой процесс.
        SessionNotFoundError — возобновляемой сессии нет в хранилище.
        SessionOpenError — профиль, режим, инструменты, хранилище или
            модель не дали открыть сессию.
        """
        models = SessionModels(self._catalog, self._types)
        try:
            agent = await self._attached(
                session_id, cwd, tools, servers, sink, resume, questions, models
            )
        except BaseException:
            await models.close()
            raise

        settings = self._settings

        return AssembledSession(agent, self._catalog, models, settings, settings.model)

    def models(self) -> Sequence[str]:
        return self._catalog.names()

    async def observe(  # noqa: PLR0913 — наблюдатель собирается всеми частями сессии
        self,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        servers: McpServerSet,
        sink: EventSink,
        questions: bool,
    ) -> ObservedSession:
        await self._present(session_id)

        return AssembledObserver(self, session_id, cwd, tools, servers, sink, questions)

    def history(self, session_id: SessionId) -> AsyncGenerator[ChainRecord, None]:
        return self._loader.records(session_id)

    async def sessions(self, cursor: str | None) -> SessionPage:
        page = await self._store.list(cursor, self.PAGE_SIZE)
        summaries: list[SessionSummary] = []
        for stored in page.sessions:
            digest = await self._digests.digest(stored.session)
            cwd = digest.cwd
            if cwd is None:
                cwd = str(self._cwd)

            summaries.append(
                SessionSummary(
                    session_id=stored.session.value,
                    cwd=cwd,
                    first_prompt=digest.first_prompt,
                    updated_at=stored.updated_at,
                )
            )

        return SessionPage(sessions=summaries, next_cursor=page.next_cursor)

    async def delete(self, session_id: SessionId) -> None:
        await self._store.delete(session_id)

    async def copy(
        self, source: SessionId, target: SessionId, up_to: str | None
    ) -> int:
        return await self._store.copy(source, target, up_to)

    async def _attached(  # noqa: PLR0913 — агент собирается всеми частями сессии
        self,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        servers: McpServerSet,
        sink: EventSink,
        resume: bool,
        questions: bool,
        models: SessionModels,
    ) -> Agent:
        """Ошибки: как у open."""
        if self._stateless:
            resume = False

        try:
            parts = self._parts(cwd, tools, servers, sink, questions, models)
        except SettingsError as exc:
            msg = f"session {session_id.value} cannot be opened: {exc}"
            raise SessionOpenError(msg) from exc

        agent = Agent(parts)
        try:
            await agent.open(session_id, resume=resume)
        except (SessionHeldError, SessionNotFoundError):
            raise
        except (
            HistoryError,
            ProfileError,
            PermissionModeError,
            ToolsError,
            SettingsError,
            LlmError,
        ) as exc:
            msg = f"session {session_id.value} cannot be opened: {exc}"
            raise SessionOpenError(msg) from exc

        return agent

    async def _present(self, session_id: SessionId) -> None:
        """У сессии есть записи в хранилище: читается первая из них.

        Ошибки:
        SessionNotFoundError — записей сессии нет.
        HistoryError — хранилище отказало.
        """
        async with aclosing(self._loader.records(session_id)) as records:
            async for _ in records:
                return

        msg = (
            f"Session '{session_id.value}' has no records in the history store, "
            "there is nothing to observe."
        )
        raise SessionNotFoundError(msg)

    def _parts(  # noqa: PLR0913 — части агента сессии перечислены все
        self,
        cwd: Path,
        tools: ToolServer | None,
        servers: McpServerSet,
        sink: EventSink,
        questions: bool,
        models: SessionModels,
    ) -> AgentParts:
        """Части агента сессии: системный промпт — промпт профиля `model`
        настроек; инструкции серверов MCP сессии ход объявляет вложением.

        Ошибки:
        SettingsError — модель профиля не собралась.
        """
        settings = self._settings

        return AgentParts(
            profile=self._catalog.profile(settings.model),
            limits=settings.limits,
            compaction=settings.compaction,
            system_prompt=settings.system_prompt_of(settings.model),
            store=self._store,
            model=models.model(settings.model),
            sink=sink,
            version=self._version,
            tools=tools,
            servers=servers,
            initial_mode=settings.permission_mode,
            allow_bypass=settings.allow_bypass,
            agent_name=settings.agent_name,
            skills=self._skills,
            environment=self._environment.snapshot(cwd),
            allowed_tools=settings.permissions.allow,
            denied_tools=settings.permissions.deny,
            record_requests=settings.record_requests,
            question_tool=questions,
        )


class StdioStreams:
    """Потоки asyncio над стандартным вводом и отданным протоколу дескриптором
    вывода: ввод читается каналом, вывод пишется с учётом противодавления."""

    LINE_LIMIT: ClassVar[int] = 50 * 1024 * 1024

    def __init__(self, stdin: BinaryIO, output_fd: int) -> None:
        self._stdin = stdin
        self._output_fd = output_fd

    async def open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=self.LINE_LIMIT)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), self._stdin
        )
        output = os.fdopen(self._output_fd, "wb", buffering=0)
        transport, protocol = await loop.connect_write_pipe(
            lambda: FlowControlMixin(loop), output
        )
        writer = asyncio.StreamWriter(transport, protocol, None, loop)

        return reader, writer


class AgentProcess:
    """Один запуск `boba acp`: настройки, проверка частей, вход ACP, остановка."""

    NAME: ClassVar[str] = "boba"
    STORE_THREAD: ClassVar[str] = "history-store"

    def __init__(  # noqa: PLR0913 — вход процесса получает все границы разом
        self,
        arguments: Arguments,
        env: Mapping[str, str],
        home: Path,
        cwd: Path,
        stdin: BinaryIO,
        output_fd: int,
        stop: ProcessStop,
        version: str,
    ) -> None:
        self._version = version
        self._env = env
        self._cwd = cwd
        self._settings = Settings(arguments, env, home, cwd)
        self._streams = StdioStreams(stdin, output_fd)
        self._stop = stop
        self._environment = Environment(env)
        # состав провайдеров объявлен явно (план, раздел 5.20): в одном файле
        # метаданных дистрибутивов и их entry points нет
        self._types = LlmProviderTypes.declare([OPENAI, OLLAMA, ONNX])
        self._providers = LlmProviders(self._types)
        self._store_thread = LoopThread(self.STORE_THREAD)

    async def run(self) -> None:
        """Всё проверяется до чтения ввода: настройки, профиль, хранилище,
        скиллы; затем вход обслуживает соединение до конца ввода или сигнала,
        после чего закрываются сессии, провайдеры моделей и процессные ресурсы
        (пулы истории Postgres: воркер psycopg_pool поглощает отмену задач
        asyncio.run, поэтому процесс закрывает их сам до выхода из main).
        Переменные ключа `env` настроек
        процесс ставит себе до сборки частей: их читают сами libpq и libkrb5.

        Ошибки:
        SystemExit — запуск не состоялся; текст называет причину.
        """
        try:
            settings = self._settings.effective()
            os.environ.update(settings.env)
            store = self._store(settings)
        except SettingsError as exc:
            raise SystemExit(str(exc)) from exc

        self._store_thread.start()
        threaded = ThreadedHistoryStore(store, self._store_thread)
        try:
            await self._serve(settings, threaded)
        finally:
            await threaded.close()
            await self._store_thread.run(AsyncPostgresPool.close_all())
            await self._store_thread.stop()

    async def _serve(self, settings: EffectiveSettings, store: HistoryStore) -> None:
        """Ошибки:
        SystemExit — профиль, модель или скиллы не собрались.
        """
        try:
            assembly = self._assembly(settings, store)
        except SettingsError as exc:
            raise SystemExit(str(exc)) from exc

        info = schema.Implementation(name=self.NAME, version=self._version)
        threads = SessionThreads(settings.sessions.max_open)
        agent = AcpAgent(assembly, info, settings.mcp, threads)
        reader, writer = await self._streams.open()
        side = AgentSide(agent.attach, info, BatchTransport(reader, writer))
        self._stop.on_stop(self._stopper(agent))
        try:
            await side.serve()
        finally:
            await agent.close_all()
            await self._providers.aclose()

    @staticmethod
    def _stopper(agent: AcpAgent) -> StopAction:
        """Действие остановки по сигналу: ходы прерываются причиной SHUTDOWN;
        работу отменяет ProcessStop, сессии закрываются после."""

        def stop(received: signal.Signals) -> None:
            logger.info("process: stopping on %s", received.name)
            agent.shutdown(StopReason.SHUTDOWN)

        return stop

    def _assembly(
        self, settings: EffectiveSettings, store: HistoryStore
    ) -> AgentAssembly:
        """Профили каталога собираются все, модель — только выбранная: она
        нужна первой сессии, остальные поднимаются при переключении.

        Ошибки:
        SettingsError — профиль, модель, хранилище или скиллы не собрались.
        """
        settings.selected_model()
        catalog = ModelCatalog(settings, self._env, self._providers)
        catalog.model(settings.model)
        try:
            skills = self._skills(settings)
        except SkillsError as exc:
            raise SettingsError(str(exc)) from exc

        return AgentAssembly(
            settings,
            catalog,
            self._types,
            store,
            skills,
            self._environment,
            self._cwd,
            self._version,
        )

    def _store(self, settings: EffectiveSettings) -> HistoryStore:
        """Ошибки:
        SettingsError — строка подключения Postgres не задана или не годится.
        """
        history = settings.history
        if isinstance(history, PostgresHistorySettings):
            postgres = PostgresBuilder(self._env).config(history)
            return PostgresHistoryStore(postgres, history.db_schema, history.owner)

        if isinstance(history, MemoryHistorySettings):
            return MemoryHistoryStore()

        return JsonlHistoryStore(Path(history.root))

    def _skills(self, settings: EffectiveSettings) -> Sequence[Skill]:
        """Ошибки:
        SkillsError — каталог скиллов не читается.
        """
        if settings.skills_dir is None:
            return ()

        return SkillsDir(self._cwd / settings.skills_dir).read()


async def main(argv: Sequence[str] | None = None) -> None:
    """Ошибки:
    SystemExit — запуск не состоялся или самопроверка не прошла.
    """
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
    if argv is None:
        argv = sys.argv[1:]

    invocation = ArgumentParser().parse(argv)
    if invocation.command is Command.SELF_CHECK:
        try:
            checked = PackageDataCheck().run()
        except PackageDataError as exc:
            raise SystemExit(str(exc)) from exc

        sys.stdout.write(f"{checked}\n")
        return

    if invocation.command is Command.SETTINGS_SCHEMA:
        document = json.dumps(SettingsSchema().document(), indent=2, ensure_ascii=False)
        sys.stdout.write(f"{document}\n")
        return

    installed = Installed().version()
    output_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    stop = ProcessStop()
    process = AgentProcess(
        invocation.arguments,
        dict(os.environ),
        Path.home(),
        Path.cwd(),
        sys.stdin.buffer,
        output_fd,
        stop,
        installed,
    )
    await stop.run(process.run())


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
