"""Entry-point boba-cli-agent-run."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

with contextlib.suppress(ImportError):
    import readline  # noqa: F401  # pyright: ignore[reportUnusedImport]
    # side-effect: подключает редактирование строки и историю в input().

from boba.agent import Agent, AgentBuilder, AgentConfig, InMemoryMessageService
from boba.agent.models import AgentRequest
from boba.cli.agent_run.config import AgentRunConfig
from boba.cli.agent_run.console_sink import ConsoleSink
from boba.cli.agent_run.infra import (
    AppCoreConfig,
    configure_logging,
)
from boba.config.bundle import ConfigBundle
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.llm.models import RequestId
from boba.llm.observer import CompositeLLMRequestObserver
from boba.patterns import ConverterInputError
from boba.plugin import ExtensionContext as PluginCtx
from boba.plugin import install_plugins
from boba.plugin.discovery import discover_plugins
from boba.prompt.providers import PromptLoader, PromptsConfig
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    OpenAIChatVisitor,
    OpenAIConfig,
    create_llm_source,
)
from boba.tools.domain import ToolContext
from boba.tools.framework import ToolsService
from boba.workspace.contract import (
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.workspace.fs import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    WorkspaceLayout,
)

_REPL_EXIT_COMMANDS = frozenset({"/exit", "/quit", ":q"})


def main() -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    try:
        bundle = _build_bundle()
    except ConverterInputError as e:
        print(f"error: {e}", file=sys.stderr)  # noqa: T201
        return 2
    return _run(bundle)


def _build_bundle() -> ConfigBundle:
    """ConfigBundle для всего: core-DTO + Plugin-протокол."""
    return ConfigBundle.from_sources(
        [
            CliSource(),
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(),
            TomlSource(),
        ]
    )


def _run(bundle: ConfigBundle) -> int:
    """Собирает агента и либо прогоняет один запрос, либо запускает REPL."""
    core = bundle.get(AppCoreConfig, "app")
    workspaces = bundle.get(WorkspaceLayout, "workspaces")
    llm_cfg = bundle.get(OpenAIConfig, "provider.openai")
    prompts = bundle.get(PromptsConfig, "prompts")
    agent_config = bundle.get(AgentConfig, "agent")
    run_cfg = bundle.get(AgentRunConfig, "agent_run")
    configure_logging(core.log_level, core.log_file)

    workspace_id = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000001")

    prompt_workspace = FsPromptWorkspaceRegistry(
        root=Path(prompts.dir),
    ).get_or_create(PromptWorkspaceId("prompts"))
    prompt_loader = PromptLoader(prompt_workspace)

    with ToolsService.from_sources(
        install_plugins(bundle, discover_plugins(), PluginCtx()),
    ) as tools_service:
        project_workspace = FsProjectWorkspaceRegistry(
            base_dir=Path(workspaces.base_dir),
            subdir=workspaces.user_subdir,
        ).get_or_create(workspace_id)

        history_workspace = FsHistoryWorkspaceRegistry(
            base_dir=Path(workspaces.base_dir),
            subdir=workspaces.system_subdir,
        ).get_or_create(workspace_id)

        observer = CompositeLLMRequestObserver(
            [
                CurlTraceChatCompletionObserver(
                    history_workspace,
                    response_chunks=False,
                ),
            ]
        )
        llm_source = create_llm_source(llm_cfg, observer)

        message_service = InMemoryMessageService()
        agent = (
            AgentBuilder()
            .with_llm(llm_source)
            .with_tools(tools_service)
            .with_tool_result_visitor(OpenAIChatVisitor())
            .with_messages(message_service)
            .with_prompts(prompt_loader.prompt_providers())
            .with_config(agent_config)
            .build(
                sink=ConsoleSink(sys.stdout, sys.stderr),
                tool_ctx=ToolContext(project_workspace=project_workspace),
            )
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
