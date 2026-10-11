"""Вход процесса агента: команда `boba acp --settings <файл>`.

Разбирает аргументы, собирает действующие настройки (Settings), на старте
проверяет профиль модели, хранилище истории и скиллы и отдаёт ACP-серверу
(boba.cli.acp) сборку сессий: на каждую `session/new` — агент ядра с
серверами MCP из запроса и из `.mcp.json` проекта. Стандартный вывод с
самого начала отдан протоколу: дескриптор 1 перенаправлен в стандартный
поток ошибок, и всё, что пишет туда кто-то ещё (лог, чужие библиотеки),
уходит в stderr. Конец ввода закрывает сессии и сбрасывает журналы;
`SIGTERM` и `SIGINT` прерывают ходы причиной SHUTDOWN, выход 0.

Ошибки:
SystemExit — настройки, профиль модели, хранилище или скиллы не годятся
    для запуска; текст называет причину, код выхода 1.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import signal
import sys
import time
from asyncio.streams import FlowControlMixin
from collections.abc import Mapping, Sequence
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import BinaryIO, ClassVar

from boba.agent.agent import Agent, AgentParts
from boba.agent.attachments import EnvironmentSnapshot
from boba.agent.events import EventSink, ToolProgressEvent
from boba.agent.history import (
    HistoryError,
    HistoryStore,
    MemoryHistoryStore,
    SessionId,
)
from boba.agent.permissions import PermissionModeError
from boba.agent.profile import ModelProfile, ProfileError
from boba.agent.skills import Skill, SkillsDir, SkillsError
from boba.agent.tools import ToolsError
from boba.cancellation import ProcessStop, StopAction, StopReason
from boba.cli.acp import (
    AcpAgent,
    AcpServer,
    OpenedSession,
    SessionAssembly,
    SessionOpenError,
    SessionRefusedError,
)
from boba.cli.settings import (
    Arguments,
    EffectiveSettings,
    EnvName,
    McpServerBuilder,
    McpServerJson,
    MemoryHistorySettings,
    PostgresBuilder,
    PostgresHistorySettings,
    ProfileBuilder,
    Settings,
    SettingsError,
    SettingsSource,
)
from boba.history.jsonl import JsonlHistoryStore
from boba.history.postgres import PostgresHistoryStore
from boba.identity.context import CallContexts
from boba.llm.chat import ChatModel, LlmError
from boba.llm.http.ollama import MANIFEST as OLLAMA
from boba.llm.http.openai import MANIFEST as OPENAI
from boba.llm.onnx.chat import MANIFEST as ONNX
from boba.llm.providers import LlmProviders, LlmProvidersError, LlmProviderTypes
from boba.mcp_client.client import (
    CallSignals,
    McpClientError,
    McpToolServer,
    NamedBlocks,
    ProgressListener,
)
from boba.toolkit.dag import DagNode, NodeOutcome, ToolCard, ToolServer
from boba.toolrun.hosted import DirectCalls

logger = logging.getLogger(__name__)

__all__ = [
    "AgentAssembly",
    "AgentProcess",
    "ArgumentParser",
    "Command",
    "Installed",
    "cli",
    "main",
]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class Command(StrEnum):
    """Команды `boba`."""

    ACP = "acp"


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
    """Командная строка `boba acp …` → Arguments (план, раздел 5.21). Секретов
    в аргументах нет: ключи и строки подключения живут в файле `--settings`."""

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

    def parse(self, argv: Sequence[str]) -> Arguments:
        space = self._parser.parse_args(list(argv))
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

        return Arguments(
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


class ManyServers(ToolServer):
    """Реализация ToolServer над несколькими серверами MCP: карточки всех,
    вызов уходит тому, чей инструмент. Создаёт сборка сессии."""

    def __init__(self, servers: Mapping[str, McpToolServer]) -> None:
        self._servers = dict(servers)
        self._owner: dict[str, McpToolServer] = {}
        self._cards: list[ToolCard] = []
        for name, server in self._servers.items():
            for card in server.tools():
                taken = self._owner.get(card.name)
                if taken is not None:
                    msg = (
                        f"MCP servers offer the same tool name '{card.name}'. Give one "
                        "of them a different prefix or disable it in settings."
                    )
                    raise SettingsError(msg)

                self._owner[card.name] = server
                self._cards.append(card)

            logger.info("mcp: server %s offers %d tool(s)", name, len(server.tools()))

    def tools(self) -> Sequence[ToolCard]:
        return self._cards

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return {}

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        pending: list[asyncio.Future[NodeOutcome]] = []
        for call in calls:
            owner = self._owner[call.tool]
            submitted = await owner.submit([call])
            pending.append(submitted[0])

        return pending

    async def close(self) -> None:
        for server in self._servers.values():
            await server.close()


class CallProgressRelay(ProgressListener):
    """Реализация ProgressListener одного вызова: отчёт сервера → событие
    ToolProgressEvent ядра в приёмник сессии."""

    def __init__(self, sink: EventSink, tool_use_id: str) -> None:
        self._sink = sink
        self._tool_use_id = tool_use_id
        self._started = time.monotonic()

    async def progressed(self, done: float, total: float | None, text: str) -> None:
        await self._sink.emit(
            ToolProgressEvent(
                tool_use_id=self._tool_use_id,
                message=text,
                progress=done,
                total=total,
                elapsed_sec=time.monotonic() - self._started,
            )
        )


class ProgressSignals(CallSignals):
    """Реализация CallSignals сессии: приёмник отчётов на каждый вызов.
    Ключ узла вызова — идентификатор блока `tool_use`, под ним клиент ведёт
    вызов в ленте."""

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink

    def listener(self, server: str, call: DagNode) -> ProgressListener:
        return CallProgressRelay(self._sink, call.key)


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


class AssembledSession(OpenedSession):
    """Реализация OpenedSession: агент и серверы MCP его сессии."""

    def __init__(self, agent: Agent, servers: ManyServers | None) -> None:
        self._agent = agent
        self._servers = servers

    @property
    def agent(self) -> Agent:
        return self._agent

    async def close(self) -> None:
        await self._agent.close()
        if self._servers is not None:
            await self._servers.close()


class AgentAssembly(SessionAssembly):
    """Реализация SessionAssembly из действующих настроек процесса.

    Профиль, модель, хранилище и скиллы проверены на старте и общие для
    всех сессий; на сессию собираются серверы MCP (из `.mcp.json` проекта и
    из запроса, запрос побеждает по имени), снимок окружения с `cwd`
    сессии и агент ядра. Собственный инструмент «вопрос пользователю» в
    ACP не предлагается: формы вопроса у клиента нет.
    """

    def __init__(  # noqa: PLR0913 — сборка получает все проверенные части разом
        self,
        settings: EffectiveSettings,
        profile: ModelProfile,
        model: ChatModel,
        store: HistoryStore,
        skills: Sequence[Skill],
        environment: Environment,
        version: str,
    ) -> None:
        self._settings = settings
        self._profile = profile
        self._model = model
        self._store = store
        self._skills = list(skills)
        self._environment = environment
        self._version = version
        self._builder = McpServerBuilder(settings.mcp, settings.env)

    async def open(
        self,
        session_id: SessionId,
        cwd: Path,
        servers: Mapping[str, McpServerJson],
        sink: EventSink,
    ) -> OpenedSession:
        merged: dict[str, McpServerJson] = dict(self._settings.mcp_servers)
        merged.update(servers)
        tools = await self._tool_servers(merged, sink)
        agent = Agent(self._parts(cwd, tools, sink))
        try:
            await agent.open(session_id, resume=False)
        except (
            HistoryError,
            ProfileError,
            PermissionModeError,
            ToolsError,
            SettingsError,
            LlmError,
        ) as exc:
            if tools is not None:
                await tools.close()

            msg = f"session {session_id.value} cannot be opened: {exc}"
            raise SessionOpenError(msg) from exc

        return AssembledSession(agent, tools)

    def _parts(
        self, cwd: Path, tools: ManyServers | None, sink: EventSink
    ) -> AgentParts:
        settings = self._settings

        return AgentParts(
            profile=self._profile,
            limits=settings.limits,
            compaction=settings.compaction,
            system_prompt=settings.system_prompt,
            store=self._store,
            model=self._model,
            sink=sink,
            version=self._version,
            tools=tools,
            initial_mode=settings.permission_mode,
            allow_bypass=settings.allow_bypass,
            agent_name=settings.agent_name,
            skills=self._skills,
            environment=self._environment.snapshot(cwd),
            allowed_tools=settings.permissions.allow,
            denied_tools=settings.permissions.deny,
            record_requests=settings.record_requests,
            question_tool=False,
        )

    async def _tool_servers(
        self, servers: Mapping[str, McpServerJson], sink: EventSink
    ) -> ManyServers | None:
        """Ошибки:
        SessionRefusedError — запись сервера не годится (заголовки, вид).
        SessionOpenError — сервер не поднялся или инструменты совпали по имени.
        """
        if not servers:
            return None

        opened: dict[str, McpToolServer] = {}
        for name, server in servers.items():
            try:
                config = self._builder.config(name, server)
            except SettingsError as exc:
                await self._close_all(opened)
                raise SessionRefusedError(str(exc)) from exc

            tool_server = McpToolServer(
                name,
                config,
                NamedBlocks(),
                ProgressSignals(sink),
                CallContexts(),
                None,
                DirectCalls(),
            )
            try:
                await tool_server.open()
            except McpClientError as exc:
                await self._close_all(opened)
                raise SessionOpenError(str(exc)) from exc

            opened[name] = tool_server

        try:
            return ManyServers(opened)
        except SettingsError as exc:
            await self._close_all(opened)
            raise SessionOpenError(str(exc)) from exc

    @staticmethod
    async def _close_all(opened: Mapping[str, McpToolServer]) -> None:
        for ready in opened.values():
            await ready.close()


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
    """Один запуск `boba acp`: настройки, проверка частей, сервер ACP, остановка."""

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
        self._providers = LlmProviders(LlmProviderTypes.declare([OPENAI, OLLAMA, ONNX]))

    async def run(self) -> None:
        """Всё проверяется до чтения ввода: настройки, профиль, хранилище,
        скиллы; затем сервер обслуживает соединение до конца ввода или сигнала.

        Ошибки:
        SystemExit — запуск не состоялся; текст называет причину.
        """
        try:
            settings = self._settings.effective()
            assembly = self._assembly(settings)
        except SettingsError as exc:
            raise SystemExit(str(exc)) from exc

        agent = AcpAgent(assembly, self._version)
        reader, writer = await self._streams.open()
        server = AcpServer(agent, reader, writer)
        self._stop.on_stop(self._stopper(agent, server))
        try:
            await server.serve()
        finally:
            await self._providers.aclose()

    @staticmethod
    def _stopper(agent: AcpAgent, server: AcpServer) -> StopAction:
        """Действие остановки по сигналу: ходы прерываются причиной SHUTDOWN,
        сервер закрывает сессии."""

        def stop(received: signal.Signals) -> None:
            logger.info("process: stopping on %s", received.name)
            agent.shutdown(StopReason.SHUTDOWN)
            server.stop()

        return stop

    def _assembly(self, settings: EffectiveSettings) -> AgentAssembly:
        """Ошибки:
        SettingsError — профиль, модель, хранилище или скиллы не собрались.
        """
        model = settings.selected_model()
        profile = ProfileBuilder(self._env).profile(settings.model, model)
        try:
            skills = self._skills(settings)
        except SkillsError as exc:
            raise SettingsError(str(exc)) from exc

        return AgentAssembly(
            settings,
            profile,
            self._chat_model(settings, profile),
            self._store(settings),
            skills,
            self._environment,
            self._version,
        )

    def _chat_model(
        self, settings: EffectiveSettings, profile: ModelProfile
    ) -> ChatModel:
        """Ошибки:
        SettingsError — провайдер профиля не собрался: в этой сборке нет его
            рантайма либо веса модели не загрузились.
        """
        try:
            return self._providers.chat(profile.chat)
        except (LlmError, LlmProvidersError) as exc:
            kind = settings.selected_model().provider.kind.value
            msg = (
                f"The model profile '{settings.model}' (provider kind '{kind}') cannot "
                f"be used: {exc}. If this build was made without the runtime of that "
                "provider, pick another profile or use a build that includes it."
            )
            raise SettingsError(msg) from exc

    def _store(self, settings: EffectiveSettings) -> HistoryStore:
        """Ошибки:
        SettingsError — строка подключения Postgres не задана или не годится.
        """
        history = settings.history
        if isinstance(history, PostgresHistorySettings):
            postgres = PostgresBuilder(self._env).config(history)
            return PostgresHistoryStore(postgres, history.db_schema)

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
    SystemExit — запуск не состоялся.
    """
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
    if argv is None:
        argv = sys.argv[1:]

    arguments = ArgumentParser().parse(argv)
    installed = Installed().version()
    output_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    stop = ProcessStop()
    process = AgentProcess(
        arguments,
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
