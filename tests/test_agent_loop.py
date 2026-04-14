"""Интеграционный тест AgentLoop — отправляет 'привет' через реальный LLM."""

from __future__ import annotations

from boba.domain.agent.events import AnswerToken, ThinkingToken
from boba.domain.agent.loop import AgentLoop
from boba.domain.agent.models import AgentRequest
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope


def test_agent_loop_hello() -> None:
    config = ConfigLoader().load()
    container = create_container(config)

    with request_scope(container) as request:
        loop = request.get(AgentLoop)
        agent_request = AgentRequest(query="привет", model=config.llm.model)

        print("\n--- AgentLoop events ---")
        for event in loop.run(agent_request):
            match event:
                case ThinkingToken(token=t):
                    print(f"[thinking] {t}", end="", flush=True)
                case AnswerToken(token=t):
                    print(t, end="", flush=True)
                case _:
                    print(f"\n[{type(event).__name__}] {event}")

        print("\n--- done ---")


if __name__ == "__main__":
    test_agent_loop_hello()
