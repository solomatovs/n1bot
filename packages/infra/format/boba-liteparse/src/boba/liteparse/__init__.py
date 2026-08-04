"""boba.liteparse — движок парсинга документов поверх нативного liteparse.

Пакет исполняется только в песочнице: он требует нативный liteparse и
разбирает недоверенные документы. Контрактные модели и ридеры страниц
живут в `boba.text.document` — их использует и приложение.

Ошибки: LiteParseError — парсинг не удался.
"""

from __future__ import annotations

from boba.liteparse.engine import LiteParseEngine, LiteParseReader, LocaleRetry

__all__ = ["LiteParseEngine", "LiteParseReader", "LocaleRetry"]
