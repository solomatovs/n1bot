"""Интеграционный тест AgentLoop — отправляет сообщение через реальный LLM."""

from __future__ import annotations

import sys

from boba.app.logging import configure_logging
from boba.domain.agent.meat import Agent
from boba.domain.agent.models import AgentRequest, RequestId
from boba.domain.core.workspace import UserWorkspaceManager, WorkspaceId
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope

FIXED_WORKSPACE_ID = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000001")


def test_agent_loop_hello(query: str) -> None:
    loader = ConfigLoader()
    app_config = loader.load_app()
    configure_logging(app_config.log_level)
    agent_config = loader.load_agent()
    llm_defaults = loader.load_llm_defaults()
    container = create_container(app_config, agent_config, llm_defaults)

    manager = container.get(UserWorkspaceManager)
    storage = manager.get_or_create(FIXED_WORKSPACE_ID)

    with request_scope(container, storage.workspace_id) as req:
        agent = req.get(Agent)
        request = AgentRequest(
            query=query,
            model=app_config.llm.model,
            workspace_id=storage.workspace_id,
            request_id=RequestId.new(),
        )

        agent.run(agent_config, request)


if __name__ == "__main__":
    query = " ".join(sys.argv[1:])
    test_agent_loop_hello(query)
