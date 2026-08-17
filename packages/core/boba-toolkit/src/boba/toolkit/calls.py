"""Sealed-семейство представления вызова инструмента и реестр объявлений.

Результат вызова давно описан семейством ToolResult — сам вызов рисовала
эвристика по форме словаря аргументов. Здесь инструмент объявляет, чем
является его вход: скриптом с языком подсветки, обычным json или ничем.
Рендер ленты выбирает форму match'ем по семейству, как и для результата.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from abc import ABC
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "HiddenCall",
    "JsonCall",
    "ScriptCall",
    "ToolCallView",
    "ToolCallViewBase",
    "ToolCallViews",
]


class ToolCallViewBase(BaseModel, ABC):
    """База вариантов представления вызова (тип значения — ToolCallView)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class JsonCall(ToolCallViewBase):
    """Аргументы показываются json'ом; вариант по умолчанию.

    Инструмент с кодом или спекой во входе обязан объявить ScriptCall —
    многострочный текст внутри json нечитаем, и рендер этого не чинит.
    """

    kind: Literal["json"] = "json"


class ScriptCall(ToolCallViewBase):
    """Главный аргумент — код: рисуется блоком с подсветкой языка.

    Остальные аргументы показываются следом; пустые строки опускаются.
    """

    kind: Literal["script"] = "script"
    arg: str
    """Имя аргумента со скриптом."""
    lang: str
    """Язык подсветки markdown-блока."""


class HiddenCall(ToolCallViewBase):
    """Вход шага не показывается: вызов целиком виден в его результате."""

    kind: Literal["hidden"] = "hidden"


ToolCallView: TypeAlias = Annotated[
    JsonCall | ScriptCall | HiddenCall,
    Field(discriminator="kind"),
]


class ToolCallViews:
    """Объявления представлений: заполняет сборка тулов, читает рендер ленты.

    Инструмент без объявления показывается как JsonCall — прежнее поведение.
    Повторная регистрация того же имени перезаписывает запись: тулы
    собираются на каждую сессию, и это контракт загрузки.
    """

    _VIEWS: ClassVar[dict[str, ToolCallView]] = {}

    DEFAULT: ClassVar[ToolCallView] = JsonCall()

    @classmethod
    def register(cls, tool_name: str, view: ToolCallView) -> None:
        cls._VIEWS[tool_name] = view

    @classmethod
    def of(cls, tool_name: str) -> ToolCallView:
        view = cls._VIEWS.get(tool_name)
        if view is None:
            return cls.DEFAULT

        return view

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        cls._VIEWS.clear()
