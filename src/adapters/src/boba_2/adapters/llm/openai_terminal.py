"""Терминал LLM-слоя — обращение к OpenAI-совместимому API.

Вход — :class:`LLMContext` с immutable :class:`LLMRequest`; выход —
стрим :class:`LLMEvent`. В отличие от старого ``OpenAIMiddleware``,
на этом уровне нет ``AgentContext``, ``RawLLMObserver`` и
chunk-preprocessor-фабрик — всё это (логирование, коррекция
параллельных tool_calls, retry) живёт в middleware LLM-слоя, а не в
терминале.

Контракт исключений:

- при ошибке провайдера (``openai.APIError`` / ``httpx.HTTPError``)
  терминал конвертирует её в потомка
  :class:`~boba_2.domain.llm.errors.LLMError` через
  :class:`~boba_2.adapters.llm.openai_errors.OpenAIErrorConverter`;
- уже-доменные :class:`LLMError` пробрасываются как есть — не
  переоборачиваем, чтобы ``__cause__`` не удлинялся цепочкой
  собственных wrap'ов.

Конструктор принимает **готовый** :class:`openai.OpenAI` клиент.
Это позволяет:

- в проде контейнер строит клиент один раз из
  :class:`~boba.domain.config.LLMConfig` через
  :func:`build_openai_client` и переиспользует;
- в тестах подменить клиент на стаб без доступа к private-атрибутам
  (никаких ``setattr`` на ``_client``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
import openai
from openai import OpenAI

from boba.domain.config import LLMConfig
from boba.domain.core.patterns import StreamSource
from boba_2.adapters.llm.openai_errors import OpenAIErrorConverter
from boba_2.adapters.llm.openai_request import ToOpenAIRequestConverter
from boba_2.adapters.llm.openai_response import FromOpenAIChunkConverter
from boba_2.domain.llm.errors import LLMError
from boba_2.domain.llm.events import LLMEvent, LLMRequestSent
from boba_2.domain.llm.models import LLMContext

logger = logging.getLogger(__name__)


def build_openai_client(config: LLMConfig) -> OpenAI:
    """Строит :class:`openai.OpenAI` из транспорт-конфига.

    Вынесено в отдельную функцию, чтобы контейнер и тесты могли
    ссылаться на один способ конструирования клиента.
    """
    return OpenAI(base_url=config.base_url, api_key=config.api_key)


class OpenAITerminal(StreamSource[LLMContext, LLMEvent]):
    """
    Terminal LLM-слоя, вызывающий OpenAI-совместимый API.

    ``FromOpenAIChunkConverter`` держит внутри счетчики состояния
    """

    def __init__(self, client: OpenAI) -> None:
        self._client = client
        self._to_request = ToOpenAIRequestConverter()
        self._error_converter = OpenAIErrorConverter()

    def name(self) -> str:
        return "OpenAITerminal"

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        kwargs = self._to_request.convert(ctx.request)

        try:
            response = self._client.chat.completions.create(**kwargs)
        except LLMError:
            raise
        except (openai.APIError, httpx.HTTPError) as e:
            raise self._error_converter.convert(e) from e

        yield LLMRequestSent(
            request_id=ctx.request_id,
            model=str(kwargs.get("model", "")),
            messages_count=len(kwargs.get("messages") or ()),
            has_tools=bool(kwargs.get("tools")),
        )

        decoder = FromOpenAIChunkConverter(ctx.request_id)
        try:
            yield from decoder.stream(ctx, response)
        except LLMError:
            raise
        except (openai.APIError, httpx.HTTPError) as e:
            raise self._error_converter.convert(e) from e
