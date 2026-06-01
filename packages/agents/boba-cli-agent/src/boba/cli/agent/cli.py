"""Entry-point boba-cli-agent."""

from __future__ import annotations

import sys
from pathlib import Path

from boba.agent import (
    Agent,
    AgentBuilder,
    CompactHistoryDialogView,
    InMemoryHistoryService,
    LLMPort,
    TurnBuilder,
)
from boba.agent.history import HistoryService
from boba.agent.tool_config import (
    ConfigSourcePluginToolFilter,
    ConfigSourceResolver,
    default_config_source,
)
from boba.agent.turn.reducers import (
    RememberUserQueryReducer,
)
from boba.agent.workspace_fs import FsWorkspaceShell
from boba.cli.agent.config import AgentRunConfig
from boba.cli.agent.console_sink import ConsoleSink
from boba.cli.agent.infra import configure_logging
from boba.llm.builder import LLMBuilder
from boba.patterns import ConverterInputError
from boba.provider.openai import (
    CurlTraceChatCompletionObserver,
    HttpTraceChatCompletionObserver,
    use_openai,
)
from boba.tools import ToolBuilder
from boba.workspace.contract import (
    ProjectWorkspaceShell,
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
    run_cfg = AgentRunConfig.load()
    rt = run_cfg.runtime
    configure_logging(rt.log_level, rt.log_file)

    workspace_id = WorkspaceId("cli")

    project_workspace = FsWorkspaceShell(
        workspace_id=workspace_id,
        root=Path(rt.user_workspace_dir),
    )
    history_root = Path(rt.system_workspace_dir)

    def history_workspace() -> FsWorkspaceShell[WorkspaceId]:
        # Свежий shell на каждый observer: общий root, изолированный cwd.
        return FsWorkspaceShell(workspace_id=workspace_id, root=history_root)

    llm = (
        LLMBuilder()
        .add_observer(CurlTraceChatCompletionObserver(history_workspace()))
        .add_observer(HttpTraceChatCompletionObserver(history_workspace()))
        .build(use_openai(rt.openai))
    )

    config_source = default_config_source()

    tool_registry = (
        ToolBuilder()
        .use_config_resolver(ConfigSourceResolver(config_source))
        .register_instance(project_workspace, provides=ProjectWorkspaceShell)
        .discover_plugins("boba.plugins", ConfigSourcePluginToolFilter(config_source))
        .build()
    )

    history: HistoryService = InMemoryHistoryService()

    turn = (
        TurnBuilder(rt.model)
        .system_prompt_from_dir(Path(rt.system_prompt_dir))
        .use_reducer(RememberUserQueryReducer())
        .with_history_view(
            CompactHistoryDialogView(history, max_messages=rt.max_messages),
        )
        .with_tool_catalog(tool_registry.catalog())
    )
    sampling = run_cfg.to_sampling_params()
    if sampling is not None:
        turn = turn.with_sampling(sampling)

    terminal = LLMPort(llm, turn)

    agent = (
        AgentBuilder()
        .use_history(history)
        .use_tool_executor(tool_registry.executor())
        .build(terminal)
    )

    sink = ConsoleSink(
        sys.stdout,
        sys.stderr,
        diagnostic=run_cfg.diagnostic,
    )

    try:
        if run_cfg.query is not None:
            _run_turn(agent, sink, run_cfg.query)
            return 0

        return _run_repl(agent, sink, run_cfg, history)
    finally:
        tool_registry.close()


def _run_turn(
    agent: Agent,
    sink: ConsoleSink,
    query: str,
) -> None:
    """Один ход: общий history_service хранит журнал событий между запросами."""
    for event in agent.stream(query):
        sink.handle(event)


def _run_repl(
    agent: Agent,
    sink: ConsoleSink,
    run_cfg: AgentRunConfig,
    history: HistoryService,
) -> int:
    """Интерактивный цикл: читает запрос → прогоняет агента → повторяет."""
    banner = (
        f"boba-cli-agent REPL — model={run_cfg.runtime.model}\n"
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
            history.clear()
            sys.stderr.write("(история очищена)\n")
            continue

        try:
            _run_turn(agent, sink, query)
        except KeyboardInterrupt:
            sys.stderr.write("\n(текущий ход прерван)\n")
