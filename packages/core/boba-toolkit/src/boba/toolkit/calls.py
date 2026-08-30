"""Sealed-семейство представления вызова инструмента и реестр объявлений.

Результат вызова давно описан семейством ToolResult — сам вызов рисовала
эвристика по форме словаря аргументов. Здесь инструмент объявляет, чем
является его вход: скриптом с языком подсветки, обычным json или ничем.
Рендер ленты выбирает форму match'ем по семейству, как и для результата.

ToolIntent — общая для всех инструментов подпись вызова: строку пишет LLM,
показывает её название шага ленты.

ArgView — sealed-семейство представления аргумента: инструмент объявляет вид
у поля через Annotated (CodeArg, ConnectionArg, ...), остальное ArgViews
выводит из типа поля. Виды уходят в каталог workflow; страница рендерит
аргумент виджетом по kind.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping
from enum import Enum, StrEnum
from types import UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    Union,
    get_args,
    get_origin,
)
from uuid import uuid4

from annotated_types import Ge, Gt, Le, Lt, MaxLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    SecretStr,
    TypeAdapter,
)
from pydantic.fields import FieldInfo
from pydantic_core import CoreSchema

__all__ = [
    "ArgPlacement",
    "ArgView",
    "ArgViewBase",
    "ArgViews",
    "BoolArg",
    "CodeArg",
    "ConnectionArg",
    "EnumArg",
    "HiddenCall",
    "IntentArg",
    "JsonArg",
    "JsonCall",
    "NumberArg",
    "PathArg",
    "ScriptCall",
    "SecretArg",
    "TextArg",
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


class CallIdPrefix(StrEnum):
    """Префикс id вызова по источнику: отличим от id, которые выдаёт модель."""

    API = "api-"
    WORKFLOW = "wf-"

    def new_id(self) -> str:
        return f"{self.value}{uuid4().hex}"


class ToolIntent:
    """Подпись вызова инструмента: одна строка от LLM для названия шага.

    Поле общее для всех инструментов и добавляется в схему приложением;
    тела инструментов о нём не знают и в песочницу оно не уезжает.
    """

    NAME: ClassVar[str] = "intent"

    DESCRIPTION: ClassVar[str] = (
        "Short line shown to the user as the step title: what this call does "
        "and why, in the language of the conversation. Keep it under ten words."
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
    def pop(cls, kwargs: dict[str, object]) -> str:
        """Снять подпись из kwargs вызова; не приехала — пустая строка."""
        value = kwargs.pop(cls.NAME, None)
        if not isinstance(value, str):
            return ""

        return value

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


class ArgPlacement(StrEnum):
    """Где страница показывает аргумент: строкой-портом тела, в шапке, нигде."""

    BODY = "body"
    HEADER = "header"
    HIDDEN = "hidden"


class ArgViewBase(BaseModel, ABC):
    """База видов аргумента (тип значения — ArgView).

    Экземпляр живёт в метаданных Annotated поля инструмента: там он должен
    быть прозрачен для pydantic и не подменять схему самого поля.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    placement: ArgPlacement = ArgPlacement.BODY

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        if source is cls:
            return super().__get_pydantic_core_schema__(source, handler)

        return handler(source)


class TextArg(ArgViewBase):
    """Свободный текст; вид по умолчанию для строк."""

    kind: Literal["text"] = "text"
    multiline: bool = False
    placeholder: str = ""


class CodeArg(ArgViewBase):
    """Код с подсветкой языка: главный аргумент блока."""

    kind: Literal["code"] = "code"
    lang: str


class ConnectionArg(ArgViewBase):
    """Имя подключения пользователя; family сужает список (postgres, clickhouse)."""

    kind: Literal["connection"] = "connection"
    family: str


class EnumArg(ArgViewBase):
    """Выбор из фиксированного набора."""

    kind: Literal["enum"] = "enum"
    options: tuple[str, ...]


class NumberArg(ArgViewBase):
    """Число; границы — из ограничений поля, отсутствуют — если их нет в схеме."""

    kind: Literal["number"] = "number"
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""


class BoolArg(ArgViewBase):
    kind: Literal["bool"] = "bool"


class PathArg(ArgViewBase):
    """Путь в рабочем пространстве."""

    kind: Literal["path"] = "path"


class JsonArg(ArgViewBase):
    """Структура (объект, список): показывается деревом, правится как json."""

    kind: Literal["json"] = "json"


class SecretArg(ArgViewBase):
    """Значение маскируется при показе."""

    kind: Literal["secret"] = "secret"


class IntentArg(ArgViewBase):
    """Подпись вызова: подзаголовок блока, не порт."""

    kind: Literal["intent"] = "intent"
    placement: ArgPlacement = ArgPlacement.HEADER


ArgView: TypeAlias = Annotated[
    TextArg
    | CodeArg
    | ConnectionArg
    | EnumArg
    | NumberArg
    | BoolArg
    | PathArg
    | JsonArg
    | SecretArg
    | IntentArg,
    Field(discriminator="kind"),
]


class ArgViews:
    """Вид аргумента по объявлению у поля, иначе — по его типу и ограничениям."""

    MULTILINE_CHARS: ClassVar[int] = 200

    @classmethod
    def of_field(cls, name: str, field: FieldInfo, call: ToolCallView) -> ArgView:
        declared = cls._declared(field)
        if declared is not None:
            return declared

        if name == ToolIntent.NAME:
            return IntentArg()

        if isinstance(call, ScriptCall) and call.arg == name:
            return CodeArg(lang=call.lang)

        return cls.infer(field)

    KINDS: ClassVar[tuple[type[ArgViewBase], ...]] = (
        TextArg,
        CodeArg,
        ConnectionArg,
        EnumArg,
        NumberArg,
        BoolArg,
        PathArg,
        JsonArg,
        SecretArg,
        IntentArg,
    )

    @classmethod
    def _declared(cls, field: FieldInfo) -> ArgView | None:
        for item in field.metadata:
            if isinstance(item, cls.KINDS):
                return TypeAdapter(ArgView).validate_python(item)

        return None

    @classmethod
    def infer(cls, field: FieldInfo) -> ArgView:
        """Вид по типу поля: Literal/Enum → enum, bool, числа с границами,
        SecretStr → secret, структуры → json, строка → text."""
        annotation = cls._unwrap_optional(field.annotation)

        options = cls._options(annotation)
        if options is not None:
            return EnumArg(options=options)

        if annotation in (int, float):
            return cls._number(field)

        if annotation is str:
            return cls._text(field)

        plain: dict[Any, ArgView] = {bool: BoolArg(), SecretStr: SecretArg()}
        return plain.get(annotation, JsonArg())

    @staticmethod
    def _options(annotation: Any) -> tuple[str, ...] | None:
        if get_origin(annotation) is Literal:
            return tuple(str(option) for option in get_args(annotation))

        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return tuple(str(member.value) for member in annotation)

        return None

    @staticmethod
    def _unwrap_optional(annotation: Any) -> Any:
        origin = get_origin(annotation)
        if origin is not Union and origin is not UnionType:
            return annotation

        members = [
            member for member in get_args(annotation) if member is not type(None)
        ]
        if len(members) != 1:
            return annotation

        return members[0]

    @classmethod
    def _number(cls, field: FieldInfo) -> NumberArg:
        minimum: float | None = None
        maximum: float | None = None
        for item in field.metadata:
            if isinstance(item, Ge):
                minimum = cls._bound(item.ge)
            if isinstance(item, Gt):
                minimum = cls._bound(item.gt)
            if isinstance(item, Le):
                maximum = cls._bound(item.le)
            if isinstance(item, Lt):
                maximum = cls._bound(item.lt)

        return NumberArg(minimum=minimum, maximum=maximum)

    @staticmethod
    def _bound(value: object) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)

        return None

    @classmethod
    def _text(cls, field: FieldInfo) -> TextArg:
        for item in field.metadata:
            if isinstance(item, MaxLen) and item.max_length > cls.MULTILINE_CHARS:
                return TextArg(multiline=True)

        return TextArg()
