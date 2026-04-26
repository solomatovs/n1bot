"""Реализации :class:`MessageService` — журнал сообщений диалога.

В пакете два варианта:

- :class:`InMemoryMessageService` — список в памяти, без персистенции;
- :class:`JsonLinesMessageService` — JSON-Lines поверх workspace-файла.

Пакет — отдельный pip-package; основной ``boba`` от него НЕ зависит.
Короткие импорты::

    from boba_adapter_messages import InMemoryMessageService
    from boba_adapter_messages import JsonLinesMessageService
"""

from boba_adapter_messages.in_memory import InMemoryMessageService
from boba_adapter_messages.jsonl import JsonLinesMessageService

__all__ = [
    "InMemoryMessageService",
    "JsonLinesMessageService",
]
