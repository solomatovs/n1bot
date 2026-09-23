"""SchemaReply: объект ответа из аргументов вызова либо из json в тексте."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from boba.llm.chat import (
    ChatEvent,
    ChatModel,
    ChatReply,
    ChatRequest,
    LlmError,
    ToolCall,
    ToolSpec,
)
from boba.llm.schema import SchemaReply

pytestmark = pytest.mark.anyio

SCHEMA = ToolSpec(
    name="Answer",
    description="answer by schema",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
)

SCHEMA_ANSWER = (
    '{"keywords": "kerberos cloudbeaver samba", '
    '"expanded": "настройка kerberos в cloudbeaver через samba AD", '
    '"english": "kerberos authentication in cloudbeaver with samba AD"}'
)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class ScriptedChat(ChatModel):
    """Чат-модель по сценарию: запоминает запрос, отдаёт заготовленный финал."""

    def __init__(self, content: str = "", calls: Sequence[ToolCall] = ()) -> None:
        self._reply = ChatReply(content=content, tool_calls=list(calls))
        self.requests: list[ChatRequest] = []

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        self.requests.append(request)
        yield self._reply


async def _ask(chat: ChatModel) -> dict[str, Any]:
    return dict(
        await SchemaReply(chat, {"temperature": 0}).ask("system", "user", SCHEMA)
    )


class TestRequest:
    async def test_request_carries_schema_prompts_and_no_stream(self) -> None:
        chat = ScriptedChat(content="{}")

        await _ask(chat)

        request = chat.requests[0]
        if request.reply_schema != SCHEMA:
            raise AssertionError(f"reply_schema: {request.reply_schema}")
        if request.stream:
            raise AssertionError("schema reply never streams")
        if [turn.content for turn in request.messages] != ["system", "user"]:
            raise AssertionError(f"messages: {request.messages}")
        if request.sampling != {"temperature": 0}:
            raise AssertionError(f"sampling: {request.sampling}")


class TestObjectExtraction:
    async def test_call_arguments_win(self) -> None:
        call = ToolCall(id="c1", name=SCHEMA.name, arguments={"text": "from call"})
        chat = ScriptedChat(content='{"text": "from text"}', calls=[call])

        if await _ask(chat) != {"text": "from call"}:
            raise AssertionError("аргументы вызова — первый источник объекта")

    async def test_fenced_json_is_unwrapped(self) -> None:
        chat = ScriptedChat(content=f"```json\n{SCHEMA_ANSWER}\n```")

        answer = await _ask(chat)
        if len(answer) != 3:
            raise AssertionError(f"три поля, получено {answer}")

    async def test_json_in_prose_with_nested_fence_and_tilde_fence(self) -> None:
        nested = SCHEMA_ANSWER.replace("samba AD", "samba ```AD```")
        chat = ScriptedChat(content=f"Варианты:\n~~~json\n{nested}\n~~~\nи ещё текст")

        answer = await _ask(chat)
        if "```AD```" not in answer["expanded"]:
            raise AssertionError(f"вложенный fence сохранён в значении: {answer}")

    async def test_truncated_json_is_an_error(self) -> None:
        cut = '{"keywords": "kerberos cloudbeaver", "expanded": "настройка kerb'

        with pytest.raises(LlmError, match="expected a json object"):
            await _ask(ScriptedChat(content=cut))

    async def test_plain_text_is_an_error(self) -> None:
        with pytest.raises(LlmError, match="expected a json object"):
            await _ask(ScriptedChat(content="1. first one\n2. second one"))

    async def test_empty_answer_is_an_error(self) -> None:
        with pytest.raises(LlmError):
            await _ask(ScriptedChat(content="   "))
