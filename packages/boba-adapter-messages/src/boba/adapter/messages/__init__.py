"""Реализации MessageService — журнал сообщений диалога.

В пакете два варианта:

- InMemoryMessageService — список в памяти, без персистенции;
- JsonLinesMessageService — JSON-Lines поверх workspace-файла.

Пакет — отдельный pip-package; основной boba от него НЕ зависит.
Короткие импорты::

    from boba.adapter.messages import InMemoryMessageService
    from boba.adapter.messages import JsonLinesMessageService
"""

from boba.adapter.messages.in_memory import InMemoryMessageService
from boba.adapter.messages.jsonl import JsonLinesMessageService

__all__ = [
    "InMemoryMessageService",
    "JsonLinesMessageService",
]
