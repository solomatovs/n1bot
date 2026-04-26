"""Публичный API доменного слоя boba.

Подпакеты: core (примитивы, tools, workspace), llm (ошибки, события,
модели запроса, retry), agent (ошибки, события, сообщения, prompt,
middleware-цепочка). На верхнем уровне реэкспортируются конфиг-dataclass'ы
из domain/config.py.

Имя LLMRequestSent есть в llm и в agent — это разные события; на плоском
уровне boba.domain их нельзя смешивать, используйте подпакеты.
"""

from boba.domain.config import AppConfig, LLMConfig, WorkspaceLayout

__all__ = [
    "AppConfig",
    "LLMConfig",
    "WorkspaceLayout",
]
