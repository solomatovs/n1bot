"""Интеграционный тест AgentLoop — отправляет 'привет' через реальный LLM."""

from __future__ import annotations

from boba.domain.agent.events import (
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalToken,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
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
                case GenerationStarted():
                    print("\n[generation started]")
                case ThinkingToken(token=t):
                    print(t, end="", flush=True)
                case AnswerToken(token=t):
                    print(t, end="", flush=True)
                case RefusalToken(token=t):
                    print(f"[refusal] {t}", end="", flush=True)
                case ToolCallBegin(index=i, tool_call_id=id, tool_name=name):
                    print(f"\n[tool call #{i}] {name} (id={id})")
                case ToolCallArgumentDelta(index=i, arguments=args):
                    print(f"  args: {args}", end="", flush=True)
                case GenerationDone(finish_reason=reason):
                    print(f"\n[generation done] reason={reason}")
                case _:
                    print(f"\n[{type(event).__name__}] {event}")

        print("\n--- done ---")


if __name__ == "__main__":
    test_agent_loop_hello()
