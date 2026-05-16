"""Entry-point boba-cli-agent."""

from __future__ import annotations

import sys
from pathlib import Path

from boba.agent import (
    Agent,
    AgentBuilder,
    InMemoryMessageService,
)
from boba.agent.prompt_providers import PromptLoader
from boba.agent.turn.reducers import (
    RememberUserQueryReducer,
)
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
)
from boba.llm.builder import LLMBuilder
from boba.patterns import ConverterInputError
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    use_openai,
)
from boba.workspace.contract import (
    ProjectWorkspaceShell,
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
    builder = AgentBuilder()

    app = AppConfig.load()
    run_cfg = AgentRunConfig.load()
    configure_logging(app.core.log_level, app.core.log_file)

    workspace_id = WorkspaceId("00000000-0000-0000-0000-000000000001")

    prompt_workspace = FsPromptWorkspaceRegistry(
        root=Path(app.prompts.dir),
    ).get_or_create(PromptWorkspaceId("prompts"))
    prompt_loader = PromptLoader(prompt_workspace)

    project_workspace = FsProjectWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.user_subdir,
    ).get_or_create(workspace_id)
    if not isinstance(project_workspace, ProjectWorkspaceShell):
        msg = (
            f"FsProjectWorkspaceRegistry returned {type(project_workspace).__name__}, "
            f"expected ProjectWorkspaceShell"
        )
        raise TypeError(msg)

    history_workspace = FsHistoryWorkspaceRegistry(
        base_dir=Path(app.workspaces.base_dir),
        subdir=app.workspaces.system_subdir,
    ).get_or_create(workspace_id)

    llm = (
        LLMBuilder()
        .add_observer(
            CurlTraceChatCompletionObserver(history_workspace),
        )
        .pipe(use_openai, app.openai)
        .build()
    )

    message_service = InMemoryMessageService()
    builder = (
        builder.with_llm(llm)
        .with_model(run_cfg.model)
        .with_messages(message_service)
        .use_default_turn_reducers()
        .use_turn_reducer(RememberUserQueryReducer())
        .with_prompts(prompt_loader.prompt_providers())
        .with_extension(ProjectWorkspaceShell, project_workspace)
        .use_tools_plugins_discovered()
    )

    sampling = run_cfg.to_sampling_params()
    if sampling is not None:
        builder = builder.with_sampling(sampling)

    agent = builder.build()
    sink = ConsoleSink(sys.stdout, sys.stderr)

    if run_cfg.query is not None:
        _run_turn(agent, sink, run_cfg.query)
        return 0

    return _run_repl(agent, sink, run_cfg, message_service)


def _run_turn(
    agent: Agent,
    sink: ConsoleSink,
    query: str,
) -> None:
    """Один ход: общий message_service хранит историю между запросами."""
    for event in agent.stream(query):
        sink.handle(event)


def _run_repl(
    agent: Agent,
    sink: ConsoleSink,
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
            _run_turn(agent, sink, query)
        except KeyboardInterrupt:
            sys.stderr.write("\n(текущий ход прерван)\n")
