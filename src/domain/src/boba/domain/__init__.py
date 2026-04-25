"""Публичный API доменного слоя boba.

Подпакеты:

- :mod:`boba.domain.core` — базовые примитивы (паттерны, ошибки,
  инструментарий tools, workspace).
- :mod:`boba.domain.llm` — ошибки, события, модели LLM-запроса,
  retry-middleware.
- :mod:`boba.domain.agent` — ошибки, события, сообщения, prompt,
  middleware-цепочка агент-слоя.

На верхнем уровне реэкспортируются конфигурационные dataclass-ы из
``domain/config.py``. Остальные публичные символы доступны через
подпакеты, каждый из которых делает плоский реэкспорт своего
внутреннего устройства.

Коллизии имён:

- ``LLMRequestStarted``, ``LLMRequestSent`` есть и в
  :mod:`boba.domain.llm`, и в :mod:`boba.domain.agent` — это разные
  события, их нельзя смешивать на плоском уровне ``boba.domain``;
  используйте подпакеты.
"""

from boba.domain.config import AppConfig, LLMConfig, WorkspaceLayout

__all__ = [
    "AppConfig",
    "LLMConfig",
    "WorkspaceLayout",
]
