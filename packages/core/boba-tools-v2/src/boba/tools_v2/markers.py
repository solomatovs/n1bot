"""Декларативные маркеры для DI-инжекции в `Annotated`.

Tool автор пишет:

    def __call__(
        self,
        cfg: Annotated[MyCfg, FromConfig(Scope.APP)],
        db: Annotated[DbConn, FromDI(Scope.APP)],
        tx: Annotated[Transaction, FromDI(Scope.REQUEST)],
        query: Annotated[str, "User query"],
    ) -> dict: ...

Framework на этапе registration plugin'а сканирует подписи всех tools и
provider'ов:
- параметры без маркера или с просто строкой/`Field(...)` → LLM-args
  (попадают в JSON schema, валидируются pydantic'ом);
- параметры с `FromDI(scope)` → DI-deps, резолвятся через Dishka;
- параметры с `FromConfig(scope)` → Pydantic-settings конфиги, framework
  авто-загружает их (через `BobaFlatSettings.load()` или просто
  инстанциацию) и регистрирует в DI-контексте перед сборкой контейнера.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.tools_v2.scope import Scope

__all__ = ["FromConfig", "FromDI", "InjectMarker"]


@dataclass(frozen=True)
class InjectMarker:
    """База для всех инжекционных маркеров. Несёт `scope`.

    Подклассы (`FromDI`, `FromConfig`) различают **способ заполнения**
    DI-слота: уже зарегистрированная служба (FromDI) или авто-загружаемый
    конфиг (FromConfig). Поведение разное на этапе registration, runtime
    резолюция одинаковая через `container.get(T)`.
    """

    scope: Scope


@dataclass(frozen=True)
class FromDI(InjectMarker):
    """Маркер: параметр резолвится из DI как уже зарегистрированная служба.

    Сервис должен быть зарегистрирован либо в app phase
    (`AgentBuilder.register(...)`), либо в plugin phase (через `@provides`
    в модуле плагина). Если на момент инвока сервиса нет — Dishka бросит
    `NoFactoryError`, который framework конвертирует в `ToolExecutionError`.
    """


@dataclass(frozen=True)
class FromConfig(InjectMarker):
    """Маркер: параметр — Pydantic-settings, framework авто-загружает на
    этапе registration plugin'а.

    Targeted-тип должен быть наследником `BobaFlatSettings` (или просто
    Pydantic `BaseSettings`). Framework на сборке Container'а:
    1. Соберёт все `FromConfig`-типы из подписей всех tools и provider'ов.
    2. Для каждого вызовет `cfg_type()` (Pydantic-settings подтянет env+TOML).
    3. Зарегистрирует инстанс в Dishka-context'е → доступен через `FromDI`-семантику.
    """
