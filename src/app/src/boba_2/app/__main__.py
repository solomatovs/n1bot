"""CLI-точка входа: ``python -m boba_2.app``.

Тонкая оболочка над :func:`boba_2.infra.container.create_agent`.
Использует существующий :class:`boba.infra.config.ConfigLoader` —
пока не перенесли конфиг в ``boba_2.infra.config`` (появится на
этапе S6/S7-cleanup), запускаемся с теми же env-переменными и
``BOBA_CONFIG``-файлом, что и старый ``python -m boba.app``.

Примеры::

    python -m boba_2.app --model qwen3 "Привет, мир"
    LLM_MODEL=qwen3 python -m boba_2.app "Привет, мир"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from boba.infra.config import ConfigLoader
from boba.infra.logging import configure_logging
from boba_2.adapters.in_memory_messages import InMemoryMessageService
from boba_2.domain.agent.models import AgentConfig, AgentRequest
from boba_2.domain.llm.models import RequestId, SamplingParams
from boba_2.infra.container import (
    DEFAULT_SYSTEM_PROMPT,
    AgentComponents,
    create_agent,
    create_empty_tools_service,
    default_prompt_providers,
)

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boba_2.app",
        description="Agent: prompt → LLM → stdout (loop-capable).",
    )
    parser.add_argument(
        "query",
        help="User query to send to the model.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("LLM_MODEL"),
        help="LLM model id. Defaults to env LLM_MODEL.",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="Override the default system prompt.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.model:
        sys.stderr.write(
            "error: --model is required (or set env LLM_MODEL)\n"
        )
        return 2

    loader = ConfigLoader()
    app_config = loader.load_app()
    legacy_agent_config = loader.load_agent()
    legacy_llm_defaults = loader.load_llm_defaults()
    configure_logging(app_config.log_level, app_config.log_file)

    agent_config = AgentConfig(
        max_iterations=legacy_agent_config.max_iterations,
        max_consecutive_tool_calls=(
            legacy_agent_config.max_consecutive_tool_calls
        ),
        max_consecutive_format_failures=(
            legacy_agent_config.max_consecutive_format_failures
        ),
    )

    # legacy SamplingParams → boba_2.SamplingParams (identical shape,
    # different module). tuple for ``stop``, т.к. boba_2 хранит
    # immutable.
    legacy_sampling = legacy_llm_defaults.sampling
    sampling = SamplingParams(
        temperature=legacy_sampling.temperature,
        top_p=legacy_sampling.top_p,
        max_tokens=legacy_sampling.max_tokens,
        seed=legacy_sampling.seed,
        stop=(
            tuple(legacy_sampling.stop)
            if legacy_sampling.stop is not None
            else None
        ),
        frequency_penalty=legacy_sampling.frequency_penalty,
        presence_penalty=legacy_sampling.presence_penalty,
    )

    # --system-prompt подменяет identity-провайдер в дефолтном наборе.
    # Расширенная конфигурация (Environment, Git, File, ...) — клиент
    # строит свой список и подаёт в create_agent напрямую.
    prompt_providers = default_prompt_providers(
        system_prompt=args.system_prompt or DEFAULT_SYSTEM_PROMPT,
    )

    agent = create_agent(
        llm_config=app_config.llm,
        components=AgentComponents(
            agent_config=agent_config,
            sampling=sampling,
            prompt_providers=prompt_providers,
            message_service=InMemoryMessageService(),
            tools_service=create_empty_tools_service(),
        ),
    )

    request = AgentRequest(
        query=args.query,
        model=args.model,
        request_id=RequestId.new(),
    )
    agent.run(agent_config, request)
    return 0


if __name__ == "__main__":
    sys.exit(main())
