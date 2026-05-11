"""Entry-point boba-cli-agent."""

from __future__ import annotations

import sys
from pathlib import Path

from boba.agent import (
    Agent,
    AgentBuilder,
    AgentConfig,
    AgentInput,
    InMemoryMessageService,
)
from boba.agent.orchestrator import AgentRequest
from boba.agent.prompt_providers import PromptLoader
from boba.agent.workspace_fs import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
)
from boba.cli.agent_run.config import AgentRunConfig
from boba.cli.agent_run.console_sink import ConsoleSink
from boba.cli.agent_run.infra import (
    AppConfig,
    configure_logging,
    use_toml_config,
)
from boba.llm.models import RequestId
from boba.llm.observer import CompositeLLMRequestObserver
from boba.patterns import ConverterInputError
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    OpenAIChatVisitor,
    create_llm_source,
)
from boba.tools.domain import ToolContext
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
    PromptWorkspaceId,
    WorkspaceId,
)

_REPL_EXIT_COMMANDS = frozenset({"/exit", "/quit", ":q"})


def main() -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    try:
        return _run()
    except ConverterInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run() -> int:
    """Собирает агента и либо прогоняет один запрос, либо запускает REPL."""
    builder = AgentBuilder().use_cli().use_env_file().use_env().pipe(use_toml_config)
    bundle = builder.bundle()

    app = bundle.get(AppConfig, "agent")
    run_cfg = bundle.get(AgentRunConfig, "cli")
    configure_logging(app.core.log_level, app.core.log_file)

    workspace_id = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000001")

    prompt_workspace = FsPromptWorkspaceRegistry(
        root=Path(app.prompts.dir),
    ).get_or_create(PromptWorkspaceId("prompts"))
    prompt_loader = PromptLoader(prompt_workspace)

    project_workspaces = FsProjectWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.user_subdir,
    )
    history_workspaces = FsHistoryWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.system_subdir,
    )
    history_workspace = history_workspaces.get_or_create(workspace_id)

    observer = CompositeLLMRequestObserver(
        [
            CurlTraceChatCompletionObserver(
                history_workspace,
                response_chunks=False,
            ),
        ],
    )
    llm_source = create_llm_source(app.openai, observer)

    message_service = InMemoryMessageService()
    agent = (
        builder.with_llm(llm_source)
        .with_tool_result_visitor(OpenAIChatVisitor())
        .with_messages(message_service)
        .with_prompts(prompt_loader.prompt_providers())
        .with_config(app.runtime)
        .with_extension(ProjectWorkspaceRegistry, project_workspaces)
        .with_extension(HistoryWorkspaceRegistry, history_workspaces)
        .use_tools_plugins_discovered()
        .build(tool_ctx=ToolContext(workspace_id=workspace_id))
    )
    sink = ConsoleSink(sys.stdout, sys.stderr)

    if run_cfg.query is not None:
        _run_turn(agent, sink, app.runtime, run_cfg, run_cfg.query)
        return 0

    return _run_repl(agent, sink, app.runtime, run_cfg, message_service)


def _run_turn(
    agent: Agent,
    sink: ConsoleSink,
    agent_config: AgentConfig,
    run_cfg: AgentRunConfig,
    query: str,
) -> None:
    """Один ход: свежий RequestId, общий message_service хранит историю."""
    request = AgentRequest(
        model=run_cfg.model,
        request_id=RequestId.new(),
        query=query,
        sampling=run_cfg.to_sampling_params(),
    )
    agent_input = AgentInput(request=request, config=agent_config)

    for event in agent.stream(agent_input):
        sink.handle(event)


def _run_repl(
    agent: Agent,
    sink: ConsoleSink,
    agent_config: AgentConfig,
    run_cfg: AgentRunConfig,
    message_service: InMemoryMessageService,
) -> int:
    """Интерактивный цикл: читает запрос → прогоняет агента → повторяет."""
    banner = (
        f"boba-cli-agent REPL — model={run_cfg.model}\n"
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
            _run_turn(agent, sink, agent_config, run_cfg, query)
        except KeyboardInterrupt:
            sys.stderr.write("\n(текущий ход прерван)\n")
