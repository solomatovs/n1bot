"""argparse-парсер и dispatch для команды boba-cli-agent-run.

Конфиг полностью идёт через ConfigSource-цепочку. Все источники
(Cli/Env/Toml) автономны: на старте сами читают свои каналы
(sys.argv / os.environ / BOBA_CONFIG_PATH). Никакого ручного маппинга
argparse → ConfigKey и никакой явной декларации флагов в этом файле —
имена флагов вычисляются из ConfigKey'ев секции через cli_flag_name.

Здесь argparse нужен только для --help; все значения (включая
``query``) — поля AgentRunSection, читаемые через bundle.
"""

from __future__ import annotations

import argparse
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
from boba.config.cli import CliSource
from boba.config.env import EnvFileSource, EnvSource
from boba.config.toml import TomlFileSource, TomlSource
from boba.domain.agent.models import AgentRequest
from boba.domain.core.config import ChainedConfigResolver
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


def main(argv: Sequence[str] | None = None) -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    # argv нужен только для --help/typo-validation. CliSource внутри
    # _build_factory читает sys.argv сам.
    _build_parser().parse_known_args(argv)
    _run()
    return 0


# ──────────────────────────────────────────────────────────────────────
# Argparse — только --help (других позиционных у нас нет)
# ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="boba-cli-agent-run",
        description=(
            "Run a single Boba agent query against the configured LLM. "
            "Все параметры — поля AgentRunSection, передаются через "
            "--<section>-<field> flags (см. cli_flag_name) или env/TOML. "
            "Обязательные: --agent-run-query, --agent-run-model."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Bundle assembly
# ──────────────────────────────────────────────────────────────────────


def _build_factory() -> ConfigFactory:
    """Стандартная цепочка автономных источников + регистрация секций.

    Cli > env-file > env > toml-file > toml. Adapter-секции и
    own-section (agent_run) регистрируются вручную; ext-секции —
    через discovery.
    """
    resolver = ChainedConfigResolver(
        [
            CliSource(),
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(),
            TomlSource(),
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


def _run() -> None:
    """Собирает агент с полным стеком middleware и прогоняет один запрос."""
    bundle = ConfigLoader(_build_factory()).load_bundle()
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

    agent.run(agent_config, request, run_cfg.query)
