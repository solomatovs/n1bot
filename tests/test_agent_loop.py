"""Интеграционный тест AgentLoop — отправляет сообщение через реальный LLM."""

from __future__ import annotations

import argparse

import pytest

from boba.domain.agent.meat import Agent
from boba.domain.agent.models import AgentRequest, RequestId
from boba.domain.core.workspace import ProjectWorkspaceRegistry, WorkspaceId
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope
from boba.infra.logging import configure_logging

pytestmark = pytest.mark.integration

FIXED_WORKSPACE_ID = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000001")


def _run(query: str, model: str | None) -> None:
    loader = ConfigLoader()
    app_config = loader.load_app()
    configure_logging(app_config.log_level, app_config.log_file)
    agent_config = loader.load_agent()
    llm_defaults = loader.load_llm_defaults()
    container = create_container(app_config, agent_config, llm_defaults)

    manager = container.get(ProjectWorkspaceRegistry)
    storage = manager.get_or_create(FIXED_WORKSPACE_ID)

    with request_scope(container, storage.workspace_id) as req:
        agent = req.get(Agent)
        request = AgentRequest(
            query=query,
            model=model or app_config.llm.model,
            workspace_id=storage.workspace_id,
            request_id=RequestId.new(),
        )

        agent.run(agent_config, request)


def test_agent_loop_hello(query: str) -> None:
    _run(query, model=None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=None,
        help="LiteLLM model name; без опции — значение из config.toml",
    )
    parser.add_argument("query", nargs="+", help="Сообщение пользователя")
    args = parser.parse_args()
    _run(" ".join(args.query), args.model)
