"""Интеграционный тест AgentLoop — отправляет сообщение через реальный LLM."""

from __future__ import annotations

import sys

from boba.adapters.console_sink import ConsoleSink
from boba.domain.agent.loop import AgentLoop
from boba.domain.agent.models import AgentRequest
from boba.domain.core.workspace import WorkspaceManager
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope


def test_agent_loop_hello() -> None:
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "привет"
    config = ConfigLoader().load()
    container = create_container(config)
    sink = ConsoleSink()

    manager = container.get(WorkspaceManager)
    storage = manager.create()

    try:
        with request_scope(container, storage.workspace_id) as req:
            loop = req.get(AgentLoop)

            events = loop.run(
                AgentRequest(
                    query=query,
                    model=config.llm.model,
                    workspace_id=storage.workspace_id,
                )
            )

            sink.consume(events)
    finally:
        manager.delete(storage.workspace_id)


if __name__ == "__main__":
    test_agent_loop_hello()
