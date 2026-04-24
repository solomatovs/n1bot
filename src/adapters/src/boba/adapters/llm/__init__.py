"""OpenAI-адаптер LLM-слоя.

Реэкспорт:

    from boba.adapters.llm import OpenAITerminal, OpenAIErrorConverter
"""

from boba.adapters.llm.openai_errors import (
    IsContextLengthError,
    IsServerError,
    IsStatusCode,
    OpenAIErrorConverter,
)
from boba.adapters.llm.openai_request import (
    ToOpenAIMessageConverter,
    ToOpenAIRequestConverter,
    ToOpenAIToolConverter,
)
from boba.adapters.llm.openai_response import (
    AnswerSource,
    FinishSource,
    FromOpenAIChunkConverter,
    MultiKeyReasoningExtractor,
    RefusalSource,
    RoleSource,
    ThinkingSource,
    ToolCallSource,
)
from boba.adapters.llm.openai_terminal import OpenAITerminal, build_openai_client

__all__ = [
    "AnswerSource",
    "FinishSource",
    "FromOpenAIChunkConverter",
    "IsContextLengthError",
    "IsServerError",
    "IsStatusCode",
    "MultiKeyReasoningExtractor",
    "OpenAIErrorConverter",
    "OpenAITerminal",
    "RefusalSource",
    "RoleSource",
    "ThinkingSource",
    "ToOpenAIMessageConverter",
    "ToOpenAIRequestConverter",
    "ToOpenAIToolConverter",
    "ToolCallSource",
    "build_openai_client",
]
