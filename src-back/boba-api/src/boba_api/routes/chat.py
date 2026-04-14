"""Chat endpoint — SSE streaming ответа агента."""

from __future__ import annotations

import json
from typing import Iterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from boba_app.agent.agent_loop import AgentLoop
from boba_app.session import ChatSession
from boba_domain.agent.config import AgentRequest
from boba_domain.agent.events import DocPipelineEvent, DocPipelineEventSerializer
from boba_domain.config import AppConfig
from boba_domain.di_types import WorkspaceContext

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    folder: str  # workspace folder ID
    message: str
    model: str
    max_tokens: int


@router.post("/chat")
def chat(body: ChatRequest, request: Request) -> StreamingResponse:

    def event_to_sse(event: DocPipelineEvent) -> str:
        return json.dumps(
            DocPipelineEventSerializer.to_dict(event),
            ensure_ascii=False,
        )

    container = request.app.state.container
    cfg = container.get(AppConfig)

    session = ChatSession.from_folder(cfg, body.folder)

    def generate() -> Iterator[str]:
        with container(context={WorkspaceContext: session.workspace_context}) as scope:
            agent = scope.get(AgentLoop)
            for event in agent.run(AgentRequest(
                query=body.message, model=body.model, max_tokens=body.max_tokens,
            )):
                yield f"data: {event_to_sse(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
