"""Адаптер: конвертация domain-типов в OpenAI API формат.

Граница между domain (LLMMessage) и infrastructure (openai SDK types).
"""

from __future__ import annotations

from typing import Any, Dict, Iterator

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_message_function_tool_call_param import (
    ChatCompletionMessageFunctionToolCallParam,
    Function as ToolCallFunction,
)

from boba_domain.chat.messages import LLMMessage, LLMRole


def _convert_tool_call(
    tc: Dict[str, Any],
) -> ChatCompletionMessageFunctionToolCallParam:
    """dict tool_call → типизированный TypedDict."""
    func = tc.get("function", {})
    return ChatCompletionMessageFunctionToolCallParam(
        id=tc["id"],
        type="function",
        function=ToolCallFunction(
            name=func.get("name", ""),
            arguments=func.get("arguments", ""),
        ),
    )


def to_openai_message(msg: LLMMessage) -> ChatCompletionMessageParam:
    """LLMMessage → ChatCompletionMessageParam."""
    match msg.role:
        case LLMRole.SYSTEM:
            return ChatCompletionSystemMessageParam(
                role="system",
                content=msg.content,
            )

        case LLMRole.USER:
            return ChatCompletionUserMessageParam(
                role="user",
                content=msg.content,
            )

        case LLMRole.ASSISTANT:
            result: ChatCompletionAssistantMessageParam = {"role": "assistant"}
            if msg.content:
                result["content"] = msg.content
            if msg.tool_calls is not None:
                result["tool_calls"] = [_convert_tool_call(tc) for tc in msg.tool_calls]
            return result

        case LLMRole.TOOL:
            return ChatCompletionToolMessageParam(
                role="tool",
                content=msg.content,
                tool_call_id=msg.tool_call_id or "",
            )


def to_openai_messages(
    messages: Iterator[LLMMessage],
) -> Iterator[ChatCompletionMessageParam]:
    """Iterator[LLMMessage] → Iterator[ChatCompletionMessageParam]."""
    for msg in messages:
        yield to_openai_message(msg)


def to_openai_tools(
    definitions: Iterator[Dict[str, Any]],
) -> Iterator[ChatCompletionToolParam]:
    """Iterator[tool definition dict] → Iterator[ChatCompletionToolParam]."""
    for d in definitions:
        yield ChatCompletionToolParam(type=d["type"], function=d["function"])
