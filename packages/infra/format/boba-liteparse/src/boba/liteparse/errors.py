"""Ошибка движка liteparse — единая точка для потребителей.

LiteParseError несёт сырое сообщение парсера; потребитель сам решает,
как его обернуть (doc-tool -> RuntimeError с человекочитаемым текстом,
indexer -> своя IndexingError). Наследуется от RuntimeError, чтобы
существующие `except RuntimeError` продолжали ловить.
"""

from __future__ import annotations

__all__ = ["LiteParseError"]


class LiteParseError(RuntimeError):
    """Парсинг документа движком liteparse не удался."""
