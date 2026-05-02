"""Entry-point boba-cli-agent-run."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

with contextlib.suppress(ImportError):
    import readline  # noqa: F401  # pyright: ignore[reportUnusedImport]
    # side-effect: подключает редактирование строки и историю в input().

from boba.adapter.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    WorkspacesSection,
)
from boba.adapter.messages import InMemoryMessageService
from boba.adapter.openai import (
    LLMTransportSection,
    TranscriptChatCompletionObserver,
    WireTraceChatCompletionObserver,
    create_llm_source,
)
from boba.adapter.prompt_providers import PromptLoader, PromptsSection
from boba.cli.agent_run.config import AgentRunConfig, AgentRunSection
from boba.cli.agent_run.console_sink import ConsoleSink
from boba.config.cli import CliSource
from boba.config.env import EnvFileSource, EnvSource
from boba.config.toml import CONFIG_PATH_ENV, TomlFileSource, TomlSource
from boba.domain.agent import Agent, AgentConfig
from boba.domain.agent.models import AgentRequest
from boba.domain.config import AppConfig
from boba.domain.core.tools import ToolContext
from boba.domain.core.workspace import (
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.domain.llm.models import RequestId
from boba.domain.llm.observer import CompositeLLMRequestObserver
from boba.infra import (
    AgentComponents,
    AgentSection,
    AppCoreSection,
    ConfigBundle,
    ConfigFactory,
    ExtensionContext,
    ToolPluginLoader,
    configure_logging,
    create_agent,
)
from boba.patterns import ConverterInputError

_REPL_EXIT_COMMANDS = frozenset({"/exit", "/quit", ":q"})


def main() -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    factory = _build_factory()
    try:
        return _run(factory)
    except ConverterInputError as e:
        print(f"error: {factory.format_config_error(e)}", file=sys.stderr)
        return 2


def _build_factory() -> ConfigFactory:
    """Регистрация секций и источников.

    Приоритет: cli > env-file > env > toml-file > toml.
    """
    factory = ConfigFactory()
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    factory.register(WorkspacesSection())
    factory.register(LLMTransportSection())
    factory.register(PromptsSection())
    factory.register(AgentRunSection())
    factory.discover_extension_sections()
    factory.attach_sources(
        [
            CliSource(),
            EnvFileSource(),
            EnvSource(extra_known={CONFIG_PATH_ENV}),
            TomlFileSource(),
            TomlSource(),
        ]
    )
    return factory


def _build_app_config(bundle: ConfigBundle) -> AppConfig:
    """Composition AppConfig из плоских секций."""
    core = bundle.section(AppCoreSection)
    return AppConfig(
        workspaces=bundle.section(WorkspacesSection),
        llm=bundle.section(LLMTransportSection),
        prompts_dir=bundle.section(PromptsSection),
        ssl_verify=core.ssl_verify,
        log_level=core.log_level,
        log_file=core.log_file,
    )


def _run(factory: ConfigFactory) -> int:
    """Собирает агента и либо прогоняет один запрос, либо запускает REPL."""
    bundle = factory.build()
    app_config = _build_app_config(bundle)
    agent_config = bundle.section(AgentSection)
    run_cfg: AgentRunConfig = bundle.section(AgentRunSection)
    configure_logging(app_config.log_level, app_config.log_file)

    workspace_id = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000001")

    prompt_workspace = FsPromptWorkspaceRegistry(
        root=Path(app_config.prompts_dir),
    ).get_or_create(PromptWorkspaceId("prompts"))
    prompt_loader = PromptLoader(prompt_workspace)

    tool_loader = ToolPluginLoader(
        ExtensionContext(config=bundle),
        tool_spec=agent_config.tool_spec,
    )

    project_workspace = FsProjectWorkspaceRegistry(
        base_dir=Path(app_config.workspaces.base_dir),
        subdir=app_config.workspaces.user_subdir,
    ).get_or_create(workspace_id)

    history_workspace = FsHistoryWorkspaceRegistry(
        base_dir=Path(app_config.workspaces.base_dir),
        subdir=app_config.workspaces.system_subdir,
    ).get_or_create(workspace_id)

    observer = CompositeLLMRequestObserver(
        [
            WireTraceChatCompletionObserver(history_workspace),
            TranscriptChatCompletionObserver(history_workspace),
        ]
    )
    llm_source = create_llm_source(app_config.llm, observer)

    message_service = InMemoryMessageService()
    agent = create_agent(
        llm_source=llm_source,
        components=AgentComponents(
            agent_config=agent_config,
            prompt_providers=prompt_loader.prompt_providers(),
            message_service=message_service,
            tools_service=tool_loader.tools_service(),
        ),
        tool_ctx=ToolContext(project_workspace=project_workspace),
        sink=ConsoleSink(sys.stdout, sys.stderr),
    )

    if run_cfg.query is not None:
        _run_turn(agent, agent_config, run_cfg, run_cfg.query)
        return 0

    return _run_repl(agent, agent_config, run_cfg, message_service)


def _run_turn(
    agent: Agent,
    agent_config: AgentConfig,
    run_cfg: AgentRunConfig,
    query: str,
) -> None:
    """Один ход: свежий RequestId, общий message_service хранит историю."""
    request = AgentRequest(
        model=run_cfg.model,
        request_id=RequestId.new(),
        sampling=run_cfg.to_sampling_params(),
    )
    agent.run(agent_config, request, query)


def _run_repl(
    agent: Agent,
    agent_config: AgentConfig,
    run_cfg: AgentRunConfig,
    message_service: InMemoryMessageService,
) -> int:
    """Интерактивный цикл: читает запрос → прогоняет агента → повторяет."""
    banner = (
        f"boba-cli-agent-run REPL — model={run_cfg.model}\n"
        f"  /exit, /quit, :q — выход\n"
        f"  /clear           — сбросить историю диалога\n"
    )
    sys.stderr.write(banner)
    sys.stderr.flush()

    while True:
        try:
            line = input("» ")
        except EOFError:
            sys.stderr.write("\n")
            return 0
        except KeyboardInterrupt:
            sys.stderr.write("\n")
            continue

        query = line.strip()
        if not query:
            continue
        if query in _REPL_EXIT_COMMANDS:
            return 0
        if query == "/clear":
            message_service.clear()
            sys.stderr.write("(история очищена)\n")
            continue

        try:
            _run_turn(agent, agent_config, run_cfg, query)
        except KeyboardInterrupt:
            sys.stderr.write("\n(текущий ход прерван)\n")
