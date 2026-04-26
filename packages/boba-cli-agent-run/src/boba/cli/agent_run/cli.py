"""argparse-парсер и dispatch для команды boba-cli-agent-run.

Конфиг полностью идёт через ConfigSource-цепочку. CLI-флаги
декларируются как кортеж CliFlag и обрабатываются пакетом
boba.config.cli (add_to_parser строит argparse-args; from_namespace
собирает CliArgsSource из распарсенного Namespace). Никакого
ручного маппинга argparse → ConfigKey в этом файле.

Позиционный query — это вход одного запроса (не конфиг), идёт
напрямую в agent.run(...).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from boba.adapter.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    WorkspacesSection,
)
from boba.adapter.messages import InMemoryMessageService
from boba.adapter.openai import (
    CompositeRawLLMObserver,
    FileContentObserver,
    FileRawLLMObserver,
    LLMTransportSection,
    create_llm_source,
)
from boba.adapter.prompt_providers import PromptLoader, PromptsSection
from boba.cli.agent_run.config import AgentRunConfig, AgentRunSection
from boba.cli.agent_run.console_sink import ConsoleSink
from boba.config.cli import CliFlag, add_to_parser, from_namespace
from boba.config.env import EnvFileSource, EnvSource
from boba.config.toml import (
    CONFIG_PATH_ENV,
    TomlFileSource,
    TomlSource,
    load_toml,
)
from boba.domain.agent.models import AgentRequest
from boba.domain.core.config import ChainedConfigResolver, ConfigKey
from boba.domain.core.tools import ToolContext
from boba.domain.core.workspace import (
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.domain.llm.models import RequestId
from boba.infra import (
    AgentComponents,
    AgentSection,
    AppCoreSection,
    ConfigFactory,
    ConfigLoader,
    ExtensionContext,
    ToolPluginLoader,
    configure_logging,
    create_agent,
)

# Декларативный список CLI-флагов: пакет boba.config.cli использует его и
# для add_to_parser (генерация argparse-аргументов), и для from_namespace
# (сборка CliArgsSource из распарсенного Namespace). dest-имена выводятся
# из long-флагов автоматически (--max-tokens → max_tokens).
_FLAGS: tuple[CliFlag, ...] = (
    CliFlag(
        ConfigKey("agent_run", "model"),
        help="LLM model (overrides BOBA_AGENT_RUN_MODEL / [agent_run] model).",
    ),
    CliFlag(ConfigKey("agent_run", "temperature")),
    CliFlag(ConfigKey("agent_run", "top_p")),
    CliFlag(ConfigKey("agent_run", "max_tokens")),
    CliFlag(ConfigKey("agent_run", "seed")),
    CliFlag(
        ConfigKey("agent_run", "stop"),
        action="append",
        help="Stop sequence; flag can be repeated for multiple values.",
    ),
    CliFlag(ConfigKey("agent_run", "frequency_penalty")),
    CliFlag(ConfigKey("agent_run", "presence_penalty")),
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    args = _build_parser().parse_args(argv)
    _run(args)
    return 0


# ──────────────────────────────────────────────────────────────────────
# Argparse
# ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boba-cli-agent-run",
        description=(
            "Run a single Boba agent query against the configured LLM. "
            "Outputs all events to stdout/stderr via ConsoleSink."
        ),
    )
    add_to_parser(parser, _FLAGS)
    parser.add_argument("query", nargs="+", help="User query (positional).")
    return parser


# ──────────────────────────────────────────────────────────────────────
# Bundle assembly
# ──────────────────────────────────────────────────────────────────────


def _build_factory(args: argparse.Namespace) -> ConfigFactory:
    """Стандартная цепочка источников + регистрация секций.

    Цепочка: CLI > env-file > env > toml-file > toml. Adapter-секции
    и own-section (agent_run) регистрируются вручную; ext-секции —
    через discovery.
    """
    toml_data = load_toml(os.environ.get(CONFIG_PATH_ENV))
    resolver = ChainedConfigResolver(
        [
            from_namespace(args, _FLAGS),
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(toml_data),
            TomlSource(toml_data),
        ]
    )
    factory = ConfigFactory(resolver)
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    factory.register(WorkspacesSection())
    factory.register(LLMTransportSection())
    factory.register(PromptsSection())
    factory.register(AgentRunSection())
    factory.discover_extension_sections()
    return factory


# ──────────────────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────────────────


def _run(args: argparse.Namespace) -> None:
    """Собирает агент с полным стеком middleware и прогоняет один запрос."""
    bundle = ConfigLoader(_build_factory(args)).load_bundle()
    app_config = bundle.app
    agent_config = bundle.agent
    run_cfg: AgentRunConfig = bundle.section(AgentRunSection)
    configure_logging(app_config.log_level, app_config.log_file)

    workspace_id = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000001")

    prompt_workspace = FsPromptWorkspaceRegistry(
        root=Path(app_config.prompts_dir),
    ).get_or_create(PromptWorkspaceId("prompts"))
    prompt_loader = PromptLoader(prompt_workspace)

    tool_loader = ToolPluginLoader(ExtensionContext(config=bundle))

    project_workspace = FsProjectWorkspaceRegistry(
        base_dir=Path(app_config.workspaces.base_dir),
        subdir=app_config.workspaces.user_subdir,
    ).get_or_create(workspace_id)

    history_workspace = FsHistoryWorkspaceRegistry(
        base_dir=Path(app_config.workspaces.base_dir),
        subdir=app_config.workspaces.system_subdir,
    ).get_or_create(workspace_id)

    observer = CompositeRawLLMObserver(
        [
            FileRawLLMObserver(history_workspace),
            FileContentObserver(history_workspace),
        ]
    )
    llm_source = create_llm_source(app_config.llm, observer)

    agent = create_agent(
        llm_source=llm_source,
        components=AgentComponents(
            agent_config=agent_config,
            prompt_providers=prompt_loader.prompt_providers(),
            message_service=InMemoryMessageService(),
            tools_service=tool_loader.tools_service(),
        ),
        tool_ctx=ToolContext(project_workspace=project_workspace),
        sink=ConsoleSink(sys.stdout, sys.stderr),
    )

    request = AgentRequest(
        model=run_cfg.model,
        request_id=RequestId.new(),
        sampling=run_cfg.to_sampling_params(),
    )

    agent.run(agent_config, request, " ".join(args.query))
