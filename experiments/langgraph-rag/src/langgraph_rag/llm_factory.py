"""Фабрика BaseChatModel через LiteLLM; провайдер кодируется в имени модели."""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_litellm import ChatLiteLLM

from langgraph_rag.config import Settings


class ChatModelFactory:
    """Создаёт ChatLiteLLM для трёх ролей графа; provider задан префиксом model."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def for_classifier(self) -> BaseChatModel:
        return self._build(self._settings.classifier_model)

    def for_keyword_extractor(self) -> BaseChatModel:
        return self._build(self._settings.keyword_extractor_model)

    def for_answerer(self) -> BaseChatModel:
        return self._build(self._settings.answerer_model)

    def _build(self, model: str) -> BaseChatModel:
        kwargs: dict[str, Any] = {"model": model}
        if self._settings.litellm_api_base:
            kwargs["api_base"] = self._settings.litellm_api_base
        if self._settings.litellm_api_key:
            kwargs["api_key"] = self._settings.litellm_api_key
        return ChatLiteLLM(**kwargs)
