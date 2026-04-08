"""Поиск — параметры и ошибки."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from domain.errors import AppError


# ---------------------------------------------------------------------------
# Ошибки поиска
# ---------------------------------------------------------------------------

class RagError(AppError):
    """Ошибка RAG-пайплайна."""


class EmptyContextError(RagError):
    """Не найдено релевантных документов в векторной базе."""


class RetrievalError(AppError):
    """Ошибка поиска документов в векторном хранилище."""


@dataclass
class SearchParams:
    """Параметры поиска и генерации, управляемые пользователем."""
    # -- поиск --
    top_n: int = 12
    answers_per_variant: int = 3
    per_page: int = 1
    content_types: list[str] | None = None
    # -- multi-query --
    use_multi_query: bool = True
    mq_variants: int = 3
    k_per_variant: int = 6
    mq_prompt_template: str = "Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {query}"
    # -- генерация --
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: Optional[int] = None
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0

    def llm_kwargs(self) -> dict:
        """Параметры генерации для передачи в OpenAI API."""
        kwargs: dict = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs
