"""Ответ модели по json-схеме: один вопрос -> объект заданной формы.

Собирает запрос system + user с формой ответа и без потока, зовёт чат-модель
и достаёт объект: из аргументов вызова, которым бэкенд оформляет ответ по
схеме, либо из первого json-объекта в тексте, если модель ответила текстом.
Проверка объекта по схеме — у вызывающего: он знает свою модель ответа.

Ошибки:
LlmError — модель недоступна или в ответе нет json-объекта.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from boba.llm.chat import (
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    LlmError,
    ToolSpec,
)

__all__ = ["SchemaReply"]


class SchemaReply:
    """Вопрос модели с обязательной формой ответа."""

    def __init__(self, chat: ChatModel, sampling: Mapping[str, Any]) -> None:
        self._chat = chat
        self._sampling = dict(sampling)

    async def ask(self, system: str, user: str, schema: ToolSpec) -> Mapping[str, Any]:
        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.SYSTEM, content=system),
                ChatTurn(role=ChatRole.USER, content=user),
            ],
            reply_schema=schema,
            sampling=self._sampling,
            stream=False,
        )

        reply = await self._chat.reply(request)

        return self._object_of(reply, schema)

    def _object_of(self, reply: ChatReply, schema: ToolSpec) -> Mapping[str, Any]:
        if reply.tool_calls:
            return reply.tool_calls[0].arguments

        found = self._json_object(reply.content)
        if found is not None:
            return found

        msg = (
            f"reply by schema {schema.name}: expected a json object as call "
            f"arguments or in the text, got {reply.content[:200]!r}"
        )
        raise LlmError(msg)

    def _json_object(self, text: str) -> Mapping[str, Any] | None:
        """Первый законченный json-объект в любой обёртке; нет — None."""
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue

            # raw_decode читает один объект с позиции index, хвост текста не мешает
            try:
                value, _ = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                continue

            if isinstance(value, dict):
                return value

        return None
