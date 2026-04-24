from __future__ import annotations

import argparse

import pytest

from boba.infra.config import ConfigLoader
from boba.infra.logging import configure_logging
from boba_2.adapters.in_memory_messages import InMemoryMessageService
from boba_2.domain.agent.meat.agent import Agent
from boba_2.domain.agent.models import AgentConfig, AgentRequest
from boba_2.domain.llm.models import RequestId, SamplingParams
from boba_2.infra.container import (
    AgentComponents,
    create_agent,
    create_empty_tools_service,
    default_prompt_providers,
)

pytestmark = pytest.mark.integration


def _run(query: str, model: str) -> None:
    """Собирает агент с полным стеком middleware и прогоняет один запрос."""
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

    agent: Agent = create_agent(
        llm_config=app_config.llm,
        components=AgentComponents(
            agent_config=agent_config,
            sampling=sampling,
            prompt_providers=default_prompt_providers(),
            message_service=InMemoryMessageService(),
            tools_service=create_empty_tools_service(),
        ),
        enable_strict_content_tool_call=True,
        enable_repeated_tool_call_guard=True,
        enable_repeated_format_failure_guard=True,
    )

    request = AgentRequest(
        query=query,
        model=model,
        request_id=RequestId.new(),
    )
    agent.run(agent_config, request)


def test_agent_loop_hello(query: str, model: str) -> None:
    _run(query, model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        help="Имя модели — обязательно (системного дефолта нет)",
    )
    parser.add_argument("query", nargs="+", help="Сообщение пользователя")
    args = parser.parse_args()
    _run(" ".join(args.query), args.model)
