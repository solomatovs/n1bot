"""Декларативные маркеры для DI-инжекции в `Annotated`.

Tool автор пишет:

    def __call__(
        self,
        cfg: Annotated[MyCfg, FromConfig],
        db: Annotated[DbConn, FromDI(Scope.APP)],
        tx: Annotated[Transaction, FromDI(Scope.REQUEST)],
        query: Annotated[str, "User query"],
    ) -> dict: ...

Различия:

- `FromDI(scope)` — параметр резолвится из DI как уже зарегистрированная
  служба. Scope задаётся явно: APP — синглтон на lifetime агента;
  REQUEST — свежий инстанс на каждый `tool.invoke()`.

- `FromConfig` — параметр является Pydantic-settings DTO. Framework на
  этапе сборки контейнера сам зовёт `cfg_type()`, инжектит результат
  в `make_container(context=...)`. **Scope всегда APP** (конфиги
  читаются один раз и живут весь lifetime агента), поэтому маркер
  не несёт `scope` поля.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.tools.declarative.scope import Scope

__all__ = ["FromConfig", "FromDI"]


@dataclass(frozen=True)
class FromDI:
    """Маркер: параметр резолвится из DI как уже зарегистрированная служба.

    Сервис должен быть зарегистрирован либо в app phase
    (`AgentBuilder.register_provider(...)`), либо в plugin phase (через
    `@provides` в модуле плагина). Если на момент инвока сервиса нет —
    Dishka бросит `NoFactoryError`.
    """

    scope: Scope


@dataclass(frozen=True)
class FromConfig:
    """Маркер: параметр — pydantic-конфиг, framework авто-загружает
    и регистрирует в DI с APP-scope на этапе сборки контейнера.

    Target-тип — чистый pydantic `BaseModel` (про своё место в конфиге не знает).
    На сборке Container'а framework:
    1. Соберёт все `FromConfig`-типы из подписей tools/provider'ов вместе с их
       плагином (`tool.<plugin>` — секция, в которой объявлен tool).
    2. Для каждого вызовет `ConfigResolver.resolve(cfg_type, plugin_name)` —
       резолвер забиндит секцию плагина в модель.
    3. Зарегистрирует инстанс в Dishka-context'е → доступен как обычная
       APP-scope DI-зависимость.
    """
