"""Chat endpoint — SSE streaming ответа агента."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from boba_app.agent.agent_loop import AgentLoop
from boba_domain.agent.events import (
    AnswerToken,
    DocPipelineEvent,
    GenerationDone,
    ThinkingToken,
    ToolCallStarted,
    ToolResultReady,
)
from boba_domain.config import AppConfig
from boba_domain.di_types import FolderContext

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    folder: str
    message: str
    model: str | None = None
    chat_id: str | None = None


@router.post("/chat")
def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    container = request.app.state.container
    cfg = container.get(AppConfig)

    folder_path = Path(cfg.import_base_dir) / body.folder
    folder_path.mkdir(parents=True, exist_ok=True)
    chat_id = body.chat_id or uuid.uuid4().hex[:12]
    cfg.boba_path(folder_path).mkdir(parents=True, exist_ok=True)
    cfg.chats_dir(folder_path).mkdir(parents=True, exist_ok=True)
    history_path = cfg.chat_history_path(folder_path, chat_id)
    history_path.touch(exist_ok=True)

    model = body.model  # None → AgentLoop возьмёт из AgentConfig.default_model

    ctx = FolderContext(folder_path=folder_path, history_path=history_path)

    def generate() -> Iterator[str]:
        with container(context={FolderContext: ctx}) as scope:
            agent = scope.get(AgentLoop)
            for event in agent.run(body.message, model):
                sse_data = _event_to_sse(event)
                if sse_data is not None:
                    yield f"data: {sse_data}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def _event_to_sse(event: DocPipelineEvent) -> str | None:
    """Конвертировать pipeline event в SSE JSON."""
    match event:
        case AnswerToken(token=tok):
            return json.dumps({"type": "answer", "token": tok}, ensure_ascii=False)
        case ThinkingToken(token=tok):
            return json.dumps({"type": "thinking", "token": tok}, ensure_ascii=False)
        case ToolCallStarted(tool_name=name, arguments=args):
            return json.dumps(
                {"type": "tool_call", "name": name, "arguments": args},
                ensure_ascii=False,
            )
        case ToolResultReady(tool_name=name, content=content):
            return json.dumps(
                {"type": "tool_result", "name": name, "content": content},
                ensure_ascii=False,
            )
        case GenerationDone():
            return json.dumps({"type": "done"})
        case _:
            return None
