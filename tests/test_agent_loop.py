"""Интеграционный тест AgentLoop — отправляет сообщение через реальный LLM."""

from __future__ import annotations

import sys

from boba.domain.agent.meat import Agent
from boba.domain.agent.models import AgentRequest, RequestId
from boba.domain.core.workspace import WorkspaceManager
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope


def test_agent_loop_hello() -> None:
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "привет"
    config = ConfigLoader().load()
    container = create_container(config)

    manager = container.get(WorkspaceManager)
    storage = manager.create()

    try:
        with request_scope(container, storage.workspace_id) as req:
            agent = req.get(Agent)
            request = AgentRequest(
                query=query,
                model=config.llm.model,
                workspace_id=storage.workspace_id,
                request_id=RequestId.new(),
            )

            agent.run(config.agent, request)
    except Exception as e:
        print(f"Error during agent loop: {e}")  # noqa: T201
        raise
    finally:
        manager.delete(storage.workspace_id)


if __name__ == "__main__":
    test_agent_loop_hello()
