"""Семейство вызова инструмента: аргументы протокола LLM как модель.

ToolCallBase — база модели аргументов: фасад @tool строит наследника из
подписи тела, тул с особым показом объявляет наследника сам. Показы те же,
что у результата: llm_view (аргументы, как их прислала модель), chat_view
(вход шага ленты) и studio_view (форма задачи на странице). Показ значения
аргумента объявляется экземпляром результата в Annotated поля —
MarkdownResult(language="sql") — и рисуется этим результатом; редактор поля
для формы выводится из типа.

ToolIntent — общая для всех инструментов подпись вызова: строку пишет LLM,
показывает её название шага ленты.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import json
from abc import ABC
from collections.abc import Iterator, Mapping, Sequence
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

from boba.toolkit.ports import StreamPorts
from boba.toolkit.result import (
    ChatView,
    FieldLines,
    JsonBlock,
    MarkdownResult,
    ToolResult,
    ToolResultBase,
)

__all__ = [
    "BoolEditor",
    "CallIdPrefix",
    "ConnectionEditor",
    "FieldEditor",
    "FieldEditorBase",
    "FieldMarks",
    "FieldPlacement",
    "JsonEditor",
    "NumberEditor",
    "SecretEditor",
    "SelectEditor",
    "StudioField",
    "StudioForm",
    "TextEditor",
    "ToolCallBase",
    "ToolCallModels",
    "ToolIntent",
]


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


class CallIdPrefix(StrEnum):
    """Префикс id вызова по источнику: отличим от id, которые выдаёт модель."""

    API = "api-"
    WORKFLOW = "wf-"
    PIPELINE = "pl-"

    def new_id(self) -> str:
        return f"{self.value}{uuid4().hex}"


class FieldPlacement(StrEnum):
    """Где страница показывает поле: строкой тела, в шапке, нигде."""

    BODY = "body"
    HEADER = "header"
    HIDDEN = "hidden"


class FieldEditorBase(BaseModel, ABC):
    """База редакторов поля формы (тип значения — FieldEditor).

    Экземпляр может лежать в metadata Annotated поля: там он прозрачен для
    pydantic и схему значения не подменяет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        if source is cls:
            return super().__get_pydantic_core_schema__(source, handler)

        return handler(source)


class TextEditor(FieldEditorBase):
    """Свободный текст; редактор по умолчанию для строк."""

    editor: Literal["text"] = "text"
    multiline: bool = False
    placeholder: str = ""


class NumberEditor(FieldEditorBase):
    editor: Literal["number"] = "number"
    minimum: float | None = None
    maximum: float | None = None


class SelectEditor(FieldEditorBase):
    """Выбор из вариантов Literal или Enum."""

    editor: Literal["select"] = "select"
    options: tuple[str, ...]


class BoolEditor(FieldEditorBase):
    editor: Literal["bool"] = "bool"


class ConnectionEditor(FieldEditorBase):
    """Выбор соединения субъекта нужного семейства."""

    editor: Literal["connection"] = "connection"
    family: str


class JsonEditor(FieldEditorBase):
    """Структура: списки, словари, модели."""

    editor: Literal["json"] = "json"


class SecretEditor(FieldEditorBase):
    editor: Literal["secret"] = "secret"


FieldEditor: TypeAlias = Annotated[
    TextEditor
    | NumberEditor
    | SelectEditor
    | BoolEditor
    | ConnectionEditor
    | JsonEditor
    | SecretEditor,
    Field(discriminator="editor"),
]
"""Словарь редакторов формы: закрыт намеренно, как блоки страницы."""


class FieldMarks:
    """Маркеры полей вызова по именам классов в metadata: сравнение типов
    между процессами невозможно, langchain-маркеры toolkit не импортирует."""

    _EDITORS: ClassVar[TypeAdapter[FieldEditor]] = TypeAdapter(FieldEditor)

    INJECTED: ClassVar[frozenset[str]] = frozenset({"Injected", "InjectedToolCallId"})
    """Injected фасада и InjectedToolCallId langchain: последним обвязка
    call_id помечает поле идентификатора вызова, дописанное в схему."""
    CONNECTION: ClassVar[frozenset[str]] = frozenset({"UserConnection"})

    @classmethod
    def injected(cls, field: FieldInfo) -> bool:
        return cls._marked(field.metadata, cls.INJECTED)

    @classmethod
    def connection(cls, field: FieldInfo) -> bool:
        return cls._marked(field.metadata, cls.CONNECTION)

    @staticmethod
    def port(field: FieldInfo) -> bool:
        return StreamPorts.is_port(field.annotation)

    @staticmethod
    def display(field: FieldInfo) -> ToolResultBase | None:
        """Объявленный показ значения: экземпляр результата в metadata."""
        for item in field.metadata:
            if isinstance(item, ToolResultBase):
                return item

        return None

    @classmethod
    def editor(cls, field: FieldInfo) -> FieldEditor | None:
        """Объявленный редактор поля: ставит обвязка соединений."""
        for item in field.metadata:
            if isinstance(item, FieldEditorBase):
                return cls._EDITORS.validate_python(item)

        return None

    @staticmethod
    def _marked(metadata: Sequence[Any], markers: frozenset[str]) -> bool:
        for item in metadata:
            klass = item if isinstance(item, type) else type(item)
            names = {parent.__name__ for parent in klass.__mro__}
            if names & markers:
                return True

        return False


class StudioField(BaseModel):
    """Поле формы задачи: редактор из типа, показ значения — объявленный результат."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    required: bool = False
    placement: FieldPlacement = FieldPlacement.BODY
    editor: FieldEditor = TextEditor()
    display: ToolResult | None = None


class StudioForm(BaseModel):
    """Показ вызова на странице studio: поля формы по порядку."""

    model_config = ConfigDict(frozen=True)

    fields: Sequence[StudioField]


class ToolCallBase(BaseModel, ABC):
    """Вызов инструмента: аргументы протокола LLM как модель.

    Поля — аргументы тела, включая injected и порты: их отсеивают маркеры.
    Показы собираются из объявленных у полей результатов; наследник
    переопределяет их, когда нужен иной вид.
    """

    model_config = ConfigDict(frozen=True)

    MULTILINE_CHARS: ClassVar[int] = 200
    """Строка с потолком длиннее — многострочный редактор."""

    @classmethod
    def llm_fields(cls) -> Iterator[str]:
        """Поля, которые заполняет модель: без injected и портов."""
        for name, field in cls.model_fields.items():
            if FieldMarks.injected(field):
                continue

            if FieldMarks.port(field):
                continue

            yield name

    def llm_view(self) -> str:
        """Аргументы, как их прислала модель: JSON её полей."""
        shown = self.model_dump(mode="json", include=set(self.llm_fields()))

        return json.dumps(shown, ensure_ascii=False)

    def chat_view(self) -> ChatView:
        """Вход шага: показы аргументов по порядку полей; пусто — входа нет."""
        blocks: list[str] = []
        for result in self._argument_results():
            blocks.append(result.chat_view().markdown)

        return ChatView(markdown="\n\n".join(blocks))

    @classmethod
    def studio_view(cls) -> StudioForm:
        """Форма задачи: редактор из типа поля, показ — из объявленного результата."""
        return StudioForm(fields=list(cls._fields()))

    def _argument_results(self) -> Iterator[ToolResultBase]:
        """Значение каждого присланного поля тела результатом по его объявлению;
        дефолты, которых модель не писала, во вход не идут."""
        for name, field in type(self).model_fields.items():
            if name not in self.model_fields_set:
                continue

            if self._placement(field, name) is not FieldPlacement.BODY:
                continue

            value = getattr(self, name, None)
            if value is None:
                continue

            if isinstance(value, SecretStr):
                continue

            display = FieldMarks.display(field)
            if display is not None:
                yield display.bound(value)
                continue

            if isinstance(value, str) and not value:
                continue

            yield MarkdownResult(text=FieldLines.line(name, value))

    @classmethod
    def _fields(cls) -> Iterator[StudioField]:
        for name, field in cls.model_fields.items():
            description = field.description
            if description is None:
                description = ""

            yield StudioField(
                name=name,
                description=description,
                required=field.is_required(),
                placement=cls._placement(field, name),
                editor=cls._editor(field),
                display=FieldMarks.display(field),
            )

    @staticmethod
    def _placement(field: FieldInfo, name: str) -> FieldPlacement:
        """Injected и порты скрыты, intent в шапке, остальное в теле."""
        if FieldMarks.injected(field):
            return FieldPlacement.HIDDEN

        if FieldMarks.port(field):
            return FieldPlacement.HIDDEN

        if name == ToolIntent.NAME:
            return FieldPlacement.HEADER

        return FieldPlacement.BODY

    @classmethod
    def _editor(cls, field: FieldInfo) -> FieldEditor:
        """Редактор по объявлению, иначе по типу и ограничениям поля."""
        declared = FieldMarks.editor(field)
        if declared is not None:
            return declared

        annotation = cls._unwrap_optional(field.annotation)

        options = cls._options(annotation)
        if options is not None:
            return SelectEditor(options=options)

        if annotation in (int, float):
            return cls._number(field)

        if annotation is str:
            return cls._text(field)

        plain: dict[Any, FieldEditor] = {bool: BoolEditor(), SecretStr: SecretEditor()}

        return plain.get(annotation, JsonEditor())

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
    def _number(cls, field: FieldInfo) -> NumberEditor:
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

        return NumberEditor(minimum=minimum, maximum=maximum)

    @staticmethod
    def _bound(value: object) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)

        return None

    @classmethod
    def _text(cls, field: FieldInfo) -> TextEditor:
        for item in field.metadata:
            if isinstance(item, MaxLen) and item.max_length > cls.MULTILINE_CHARS:
                return TextEditor(multiline=True)

        return TextEditor()


class ToolCallModels:
    """Модели вызова по имени инструмента: наполняет фасад @tool, читает лента.

    Повторная регистрация имени перезаписывает запись: тулы собираются на
    каждую сессию, и это контракт загрузки.
    """

    _MODELS: ClassVar[dict[str, type[ToolCallBase]]] = {}

    @classmethod
    def register(cls, tool_name: str, model: type[ToolCallBase]) -> None:
        cls._MODELS[tool_name] = model

    @classmethod
    def call_of(cls, tool_name: str, args: Mapping[str, Any]) -> ToolCallBase:
        """Вызов по аргументам без валидации: битые аргументы показываются
        как есть. Инструмент без модели — аргументы json-текстом."""
        model = cls._MODELS.get(tool_name)
        if model is None:
            return RawCall.model_construct(args=dict(args))

        return model.model_construct(**args)

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        cls._MODELS.clear()


class RawCall(ToolCallBase):
    """Вызов инструмента без модели: аргументы показываются json-блоком."""

    args: Mapping[str, Any]

    def chat_view(self) -> ChatView:
        return MarkdownResult(
            text=JsonBlock.pretty(dict(self.args)), language="json"
        ).chat_view()
