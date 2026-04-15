"""Интеграционный тест AgentLoop — отправляет сообщение через реальный LLM."""

from __future__ import annotations

import sys

from boba.adapters.console_sink import ConsoleSink
from boba.domain.agent.loop import AgentLoop
from boba.domain.agent.models import AgentRequest
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope


def test_agent_loop_hello() -> None:
    config = ConfigLoader().load()
    container = create_container(config)
    sink = ConsoleSink()

    with request_scope(container) as request:
        loop = request.get(AgentLoop)
        query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "привет"
        agent_request = AgentRequest(query=query, model=config.llm.model)

        sink.consume(loop.run(agent_request))


if __name__ == "__main__":
    test_agent_loop_hello()
