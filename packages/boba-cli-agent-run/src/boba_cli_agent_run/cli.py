"""argparse-парсер и dispatch для команды ``boba-cli-agent-run``.

Собирает полный agent stack через :mod:`boba.infra.container` (тот же
путь что и chainlit-runtime), прогоняет один пользовательский запрос с
sink'ом на stdout/stderr и завершает процесс. Всё DI — на стороне
``boba.infra``, CLI только маппит CLI-флаги в ``SamplingParams`` и
формирует ``AgentRequest``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from boba.adapters.console_sink import ConsoleSink
from boba.adapters.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
)
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.raw_llm_observer import (
    CompositeRawLLMObserver,
    FileContentObserver,
    FileRawLLMObserver,
)
from boba.domain.agent.models import AgentRequest
from boba.domain.core.config import ChainedConfigResolver
from boba.domain.core.tools import ToolContext
from boba.domain.core.workspace import (
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.domain.llm.models import RequestId, SamplingParams
from boba.infra.config import ConfigLoader, default_config_factory
from boba.infra.container import (
    AgentComponents,
    build_prompt_providers,
    create_agent,
)
from boba.infra.logging import configure_logging
from boba.infra.prompt_loader import PromptLoader
from boba.infra.tool_plugin_loader import ExtensionContext, ToolPluginLoader
from boba_config_env import EnvFileSource, EnvSource
from boba_config_toml import (
    CONFIG_PATH_ENV,
    TomlFileSource,
    TomlSource,
    load_toml,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _run(
        query=" ".join(args.query),
        model=args.model,
        sampling=_build_sampling(args),
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boba-cli-agent-run",
        description=(
            "Run a single Boba agent query against the configured LLM. "
            "Outputs all events to stdout/stderr via ConsoleSink."
        ),
    )
    parser.add_argument(
        "--model",
        required=True,
        help="LLM model name — required (no system default).",
    )
    parser.add_argument("--temperature", type=_opt_float, default=None)
    parser.add_argument("--top-p", type=_opt_float, default=None)
    parser.add_argument("--max-tokens", type=_opt_int, default=None)
    parser.add_argument("--seed", type=_opt_int, default=None)
    parser.add_argument(
        "--stop",
        action="append",
        default=None,
        help="Stop sequence; flag can be repeated for multiple values.",
    )
    parser.add_argument("--frequency-penalty", type=_opt_float, default=None)
    parser.add_argument("--presence-penalty", type=_opt_float, default=None)
    parser.add_argument("query", nargs="+", help="User query.")
    return parser


def _opt_float(value: str) -> float | None:
    """Пустая строка → None (флаг проигнорирован); иначе ``float(value)``.

    Нужно для launch.json: пользователь может стереть значение во
    VSCode-input'е, и тогда параметр не передаётся провайдеру.
    """
    return None if value == "" else float(value)


def _opt_int(value: str) -> int | None:
    return None if value == "" else int(value)


def _build_sampling(args: argparse.Namespace) -> SamplingParams | None:
    """Собирает :class:`SamplingParams` из CLI-флагов. Если ни один не
    задан — ``None`` (провайдеру не передаётся ничего, поведение модели
    — её собственный дефолт).
    """
    fields = {
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "seed": args.seed,
        "stop": tuple(args.stop) if args.stop else None,
        "frequency_penalty": args.frequency_penalty,
        "presence_penalty": args.presence_penalty,
    }
    if all(v is None for v in fields.values()):
        return None
    return SamplingParams(**fields)


def _build_resolver() -> ChainedConfigResolver:
    """Стандартная цепочка источников для CLI: env (с file-указателем) и
    TOML (с file-указателем) — порядок: env-file > env > toml-file > toml.

    Перечислено явно, чтобы было видно, какие источники подключены
    в этом потребителе. Других потребителей это не касается — каждый
    собирает свою цепочку под свои нужды.
    """
    toml_data = load_toml(os.environ.get(CONFIG_PATH_ENV))
    return ChainedConfigResolver(
        [
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(toml_data),
            TomlSource(toml_data),
        ]
    )


def _run(query: str, model: str, sampling: SamplingParams | None) -> None:
    """Собирает агент с полным стеком middleware и прогоняет один запрос."""
    loader = ConfigLoader(default_config_factory(_build_resolver()))
    bundle = loader.load_bundle()
    app_config = bundle.app
    agent_config = bundle.agent
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

    agent = create_agent(
        llm_config=app_config.llm,
        components=AgentComponents(
            agent_config=agent_config,
            prompt_providers=build_prompt_providers(prompt_loader),
            message_service=InMemoryMessageService(),
            tools_service=tool_loader.tools_service(),
        ),
        tool_ctx=ToolContext(project_workspace=project_workspace),
        observer=CompositeRawLLMObserver(
            [
                FileRawLLMObserver(history_workspace),
                FileContentObserver(history_workspace),
            ]
        ),
        sink=ConsoleSink(sys.stdout, sys.stderr),
    )

    request = AgentRequest(
        model=model,
        request_id=RequestId.new(),
        sampling=sampling,
    )

    agent.run(agent_config, request, query)
