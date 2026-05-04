"""AuthApplier — callable, мутирующий kwargs HTTP-клиента.

Контракт между `RequestSource` (знает какие credentials есть) и `Transport`
(знает свой HTTP-клиент). RequestSource кладёт `AuthApplier` в `Request.auth`,
Transport применяет к kwargs httpx.Client / aiohttp / urllib — без знания
конкретного механизма (PAT vs Basic vs OAuth).

Чтобы добавить новый auth-механизм — пишется новый callable, transport не
меняется.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeAlias

__all__ = ["AuthApplier"]


AuthApplier: TypeAlias = Callable[[dict[str, Any]], None]
