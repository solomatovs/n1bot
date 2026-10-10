"""Вход процесса агента: команда `boba`.

Разбирает аргументы, собирает действующие настройки (Settings), строит
ядро с реализациями портов — модель через реестр провайдеров, журнал JSONL,
серверы MCP клиентом, скиллы из каталога — и ведёт обмен строками JSON
через стандартные потоки (план, разделы 5.18, 5.21). Дескриптор 1 с самого
начала отдан протоколу: всё, что пишет в него кто-то ещё (лог, чужие
библиотеки), уходит в стандартный поток ошибок. Задачи цикла событий:
обслуживание очереди (Agent.serve), читатель ввода, писатель вывода.
Конец ввода доводит ход и очередь до конца; `SIGTERM`, `SIGINT` и
`end_session` прерывают ход причиной SHUTDOWN; журнал сбрасывается, выход 0.

Ошибки:
SystemExit — настройки или журнал не годятся для запуска, сервер MCP не
    поднялся; текст называет причину, код выхода 1.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import signal
import sys
import uuid
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import BinaryIO, ClassVar

from boba.agent.agent import Agent, AgentParts
from boba.agent.attachments import EnvironmentSnapshot
from boba.agent.history import HistoryError, HistoryStore, SessionId
from boba.agent.permissions import PermissionModeError
from boba.agent.profile import ProfileError
from boba.agent.records import PermissionMode, RecordCodec
from boba.agent.skills import Skill, SkillsDir, SkillsError
from boba.agent.tools import ToolsError
from boba.cancellation import ProcessStop, StopAction, StopReason
from boba.cli.protocol import (
    EndOfInput,
    EventLines,
    InputReader,
    LineDispatcher,
    OutputWriter,
)
from boba.cli.settings import (
    Arguments,
    EffectiveSettings,
    EnvName,
    HistoryKind,
    McpServerBuilder,
    ProfileBuilder,
    Settings,
    SettingsError,
    SettingsSource,
)
from boba.history.jsonl import JsonlHistoryStore
from boba.identity.context import CallContexts
from boba.llm.http.ollama import MANIFEST as OLLAMA
from boba.llm.http.openai import MANIFEST as OPENAI
from boba.llm.onnx.chat import MANIFEST as ONNX
from boba.llm.providers import LlmProviders, LlmProviderTypes
from boba.mcp_client.client import (
    DroppedSignals,
    McpClientError,
    McpToolServer,
    NamedBlocks,
)
from boba.toolkit.dag import DagNode, NodeOutcome, ToolCard, ToolServer
from boba.toolrun.hosted import DirectCalls

logger = logging.getLogger(__name__)

__all__ = ["AgentProcess", "ArgumentParser", "Installed", "cli", "main"]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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
    """Командная строка `boba` → Arguments (план, раздел 5.21)."""

    PROG: ClassVar[str] = "boba"

    def __init__(self) -> None:
        self._parser = argparse.ArgumentParser(
            prog=self.PROG,
            description=(
                "Boba agent process: reads JSON lines from stdin, writes JSON lines "
                "to stdout. Settings come from settings.json layers, .mcp.json and "
                "the arguments below."
            ),
        )
        self._define(self._parser)

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

        mode: PermissionMode | None = None
        if space.permission_mode is not None:
            mode = PermissionMode(space.permission_mode)

        env: dict[str, str] = {}
        for pair in space.env:
            key, separator, value = str(pair).partition("=")
            if not separator or not key:
                self._parser.error(f"-e expects KEY=value, got {pair!r}")

            env[key] = value

        return Arguments(
            session=space.session,
            resume=bool(space.resume),
            model=space.model,
            permission_mode=mode,
            allow_bypass=bool(space.allow_bypass),
            system_prompt=space.system_prompt,
            system_prompt_file=space.system_prompt_file,
            append_system_prompt=space.append_system_prompt,
            append_system_prompt_file=space.append_system_prompt_file,
            skills_dir=space.skills_dir,
            max_turns=space.max_turns,
            mcp_config=tuple(space.mcp_config),
            strict_mcp_config=bool(space.strict_mcp_config),
            settings=tuple(space.settings),
            setting_sources=tuple(sources),
            policy=space.policy,
            history_root=space.history_root,
            record_requests=bool(space.record_requests),
            env=env,
        )

    @staticmethod
    def _define(parser: argparse.ArgumentParser) -> None:
        add = parser.add_argument
        add("--session", help="session id; a new one is minted when omitted")
        add("--resume", action="store_true", help="resume the session from its journal")
        add("--model", help="name of the model profile from settings 'models'")
        add(
            "--permission-mode",
            choices=[m.value for m in PermissionMode],
            help="initial permission mode of a new session",
        )
        add(
            "--allow-bypass",
            action="store_true",
            help="allow the mode bypassPermissions in this process",
        )
        add("--system-prompt", help="system prompt text")
        add("--system-prompt-file", help="file with the system prompt")
        add("--append-system-prompt", help="text appended to the system prompt")
        add("--append-system-prompt-file", help="file appended to the system prompt")
        add("--skills-dir", help="directory with <skill>/SKILL.md")
        add("--max-turns", type=int, help="model requests per turn")
        add(
            "--mcp-config",
            action="append",
            default=[],
            metavar="FILE_OR_JSON",
            help=".mcp.json file or inline JSON; repeatable",
        )
        add(
            "--strict-mcp-config",
            action="store_true",
            help="ignore the project .mcp.json, use only --mcp-config",
        )
        add(
            "--settings",
            action="append",
            default=[],
            metavar="FILE_OR_JSON",
            help="settings.json file or inline JSON layer; repeatable",
        )
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
        add(
            "-e",
            action="append",
            default=[],
            dest="env",
            metavar="KEY=value",
            help="environment for stdio MCP servers; repeatable",
        )


class ManyServers(ToolServer):
    """Реализация ToolServer над несколькими серверами MCP: карточки всех,
    вызов уходит тому, чей инструмент. Создаёт вход процесса."""

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


class Environment:
    """Снимок окружения для модели и переменные процесса, прочитанные один раз."""

    def __init__(self, env: Mapping[str, str], cwd: Path) -> None:
        self._env = env
        self._cwd = cwd

    def snapshot(self) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            working_directory=str(self._cwd),
            platform=sys.platform,
            shell=self._env.get(EnvName.SHELL.value, ""),
            os_version=platform.platform(),
        )


class AgentProcess:
    """Один запуск `boba`: настройки, сборка, обмен по потокам, остановка."""

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
        self._arguments = arguments
        self._env = env
        self._cwd = cwd
        self._settings = Settings(arguments, env, home, cwd)
        self._reader = InputReader(stdin)
        self._output_fd = output_fd
        self._stop = stop
        self._codec = RecordCodec()
        self._environment = Environment(env, cwd)
        # состав провайдеров объявлен явно (план, раздел 5.20): в одном файле
        # метаданных дистрибутивов и их entry points нет
        self._providers = LlmProviders(LlmProviderTypes.declare([OPENAI, OLLAMA, ONNX]))
        self._servers: ManyServers | None = None

    async def run(self) -> None:
        """Всё поднимается до чтения ввода: настройки, журнал, серверы MCP;
        событие `init` уходит сразу, затем читаются строки.

        Ошибки:
        SystemExit — запуск не состоялся; текст называет причину.
        """
        try:
            settings = self._settings.effective()
        except SettingsError as exc:
            raise SystemExit(str(exc)) from exc

        writer = OutputWriter(
            self._output_fd, EventLines(self._codec, settings.public())
        )
        writer.start()
        try:
            await self._serve(settings, writer)
        finally:
            await writer.stop()

    async def _serve(self, settings: EffectiveSettings, writer: OutputWriter) -> None:
        agent = await self._opened(settings, writer)
        dispatcher = LineDispatcher(agent, writer, sorted(settings.models))
        serving = asyncio.create_task(agent.serve(), name="agent-serve")
        self._stop.on_stop(self._stopper(agent, dispatcher))
        try:
            await self._reader.open()
            await self._reader.run(dispatcher)
            if dispatcher.ended is EndOfInput.EOF:
                await agent.settled()
        finally:
            await agent.close()
            await serving
            if self._servers is not None:
                await self._servers.close()

            await self._providers.aclose()

    @staticmethod
    def _stopper(agent: Agent, dispatcher: LineDispatcher) -> StopAction:
        """Действие остановки по сигналу: ход прерывается причиной SHUTDOWN,
        чтение ввода считается законченным."""

        def stop(received: signal.Signals) -> None:
            logger.info("process: stopping on %s", received.name)
            dispatcher.end(EndOfInput.END_SESSION)
            agent.interrupt(StopReason.SHUTDOWN)
            agent.queue.wake()

        return stop

    async def _opened(self, settings: EffectiveSettings, writer: OutputWriter) -> Agent:
        """Ошибки:
        SystemExit — профиль, журнал, скиллы или серверы MCP не годятся.
        """
        session_id = self._session_id()
        try:
            parts = await self._parts(settings, writer)
            agent = Agent(parts)
            await agent.open(session_id, self._arguments.resume)
        except (
            SettingsError,
            HistoryError,
            ProfileError,
            PermissionModeError,
            ToolsError,
            SkillsError,
            McpClientError,
        ) as exc:
            if self._servers is not None:
                await self._servers.close()

            raise SystemExit(str(exc)) from exc

        return agent

    def _session_id(self) -> SessionId:
        value = self._arguments.session
        if value is None:
            if self._arguments.resume:
                msg = "--resume needs --session <id> of the session to resume."
                raise SystemExit(msg)

            value = str(uuid.uuid4())

        try:
            return SessionId(value=value)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    async def _parts(
        self, settings: EffectiveSettings, writer: OutputWriter
    ) -> AgentParts:
        """Ошибки:
        SettingsError, SkillsError, McpClientError — часть не собралась.
        """
        model = settings.selected_model()
        profile = ProfileBuilder(self._env).profile(settings.model, model)
        servers = await self._tool_servers(settings)

        return AgentParts(
            profile=profile,
            limits=settings.limits,
            compaction=settings.compaction,
            system_prompt=settings.system_prompt,
            store=self._store(settings),
            model=self._providers.chat(profile.chat),
            sink=writer,
            version=self._version,
            tools=servers,
            initial_mode=settings.permission_mode,
            allow_bypass=settings.allow_bypass,
            agent_name=settings.agent_name,
            skills=self._skills(settings),
            environment=self._environment.snapshot(),
            allowed_tools=settings.permissions.allow,
            denied_tools=settings.permissions.deny,
            record_requests=settings.record_requests,
        )

    @staticmethod
    def _store(settings: EffectiveSettings) -> HistoryStore:
        history = settings.history
        if history.kind is HistoryKind.POSTGRES:
            msg = (
                "The history store 'postgres' is not available in this version; "
                'use {"kind": "jsonl", "root": ...}.'
            )
            raise SettingsError(msg)

        return JsonlHistoryStore(Path(history.root))

    def _skills(self, settings: EffectiveSettings) -> Sequence[Skill]:
        if settings.skills_dir is None:
            return ()

        return SkillsDir(self._cwd / settings.skills_dir).read()

    async def _tool_servers(self, settings: EffectiveSettings) -> ManyServers | None:
        if not settings.mcp_servers:
            return None

        builder = McpServerBuilder(settings.mcp, settings.env)
        opened: dict[str, McpToolServer] = {}
        for name, server in settings.mcp_servers.items():
            tool_server = McpToolServer(
                name,
                builder.config(name, server),
                NamedBlocks(),
                DroppedSignals(),
                CallContexts(),
                None,
                DirectCalls(),
            )
            try:
                await tool_server.open()
            except McpClientError:
                for ready in opened.values():
                    await ready.close()

                raise

            opened[name] = tool_server

        self._servers = ManyServers(opened)

        return self._servers


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
