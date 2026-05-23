"""Scope enum + маппинг на Dishka.

Provider-нейтральный enum, чтобы tool/plugin авторы не импортировали
конкретный DI-фреймворк. Маппинг на `dishka.Scope` инкапсулирован тут.
"""

from __future__ import annotations

import enum

from dishka import Scope as DishkaScope

__all__ = ["Scope", "to_dishka_scope"]


class Scope(enum.Enum):
    """Жизненный цикл инстанса службы в DI.

    `APP` — синглтон на весь lifetime агента. Идеально для тяжёлых ресурсов
    (HTTP-клиенты, DB-пулы, embedder-модели).

    `REQUEST` — свежий инстанс на каждый `tool.stream()`. Идеально для
    концепций, у которых per-call state (транзакции, request-id-bound
    объекты, временные scratch'и).
    """

    APP = "app"
    REQUEST = "request"


_TO_DISHKA: dict[Scope, DishkaScope] = {
    Scope.APP: DishkaScope.APP,
    Scope.REQUEST: DishkaScope.REQUEST,
}


def to_dishka_scope(scope: Scope) -> DishkaScope:
    """Преобразовать наш `Scope` в `dishka.Scope`. Инкапсулирует backend-выбор."""
    return _TO_DISHKA[scope]
