"""Sealed-семейство представления вызова инструмента и реестр объявлений.

Результат вызова давно описан семейством ToolResult — сам вызов рисовала
эвристика по форме словаря аргументов. Здесь инструмент объявляет, чем
является его вход: скриптом с языком подсветки, обычным json или ничем.
Рендер ленты выбирает форму match'ем по семейству, как и для результата.

ToolIntent — общая для всех инструментов подпись вызова: строку пишет LLM,
показывает её название шага ленты.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "HiddenCall",
    "JsonCall",
    "ScriptCall",
    "ToolCallView",
    "ToolCallViewBase",
    "ToolCallViews",
    "ToolIntent",
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


class ToolIntent:
    """Подпись вызова инструмента: одна строка от LLM для названия шага.

    Поле общее для всех инструментов и добавляется в схему приложением;
    тела инструментов о нём не знают и в песочницу оно не уезжает.
    """

    NAME: ClassVar[str] = "intent"

    DESCRIPTION: ClassVar[str] = (
        "Required for every call. Short line shown to the user as the step "
        "title: what this call does and why, in the language of the "
        "conversation. Keep it under ten words."
    )

    MAX_CHARS: ClassVar[int] = 160

    ELLIPSIS: ClassVar[str] = "…"

    @classmethod
    def of(cls, args: Mapping[str, Any]) -> str:
        """Подпись вызова; пустая строка — модель поле не заполнила."""
        value = args.get(cls.NAME)
        if not isinstance(value, str):
            return ""

        return cls._flat(value)

    @classmethod
    def without(cls, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """Аргументы без подписи: во вход шага и в тело инструмента она не идёт."""
        rest: dict[str, Any] = {}
        for name, value in args.items():
            if name == cls.NAME:
                continue

            rest[name] = value

        return rest

    @classmethod
    def _flat(cls, value: str) -> str:
        """Одна строка в пределах потолка: подпись живёт в названии шага."""
        text = " ".join(value.split())
        if len(text) <= cls.MAX_CHARS:
            return text

        clipped = text[: cls.MAX_CHARS].rstrip()
        return f"{clipped}{cls.ELLIPSIS}"


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
