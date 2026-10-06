"""Семейство вызова инструмента: аргументы протокола LLM как модель.

ToolCallBase — база модели аргументов: фасад @tool строит наследника из
подписи тела, тул с особым показом объявляет наследника сам. Показы те же,
что у результата: llm_view (аргументы, как их прислала модель) и chat_view
(вход шага ленты). Показ значения аргумента объявляется экземпляром
результата в Annotated поля — MarkdownResult(language="sql") — и рисуется
этим результатом.

ToolIntent — общая для всех инструментов подпись вызова: строку пишет LLM,
показывает её название шага ленты.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import json
from abc import ABC
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import (
    Any,
    ClassVar,
)
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
)
from pydantic.fields import FieldInfo

from boba.toolkit.ports import StreamPorts
from boba.toolkit.result import (
    ChatView,
    FieldLines,
    JsonBlock,
    MarkdownResult,
    ToolArtifact,
    ToolResultBase,
)

__all__ = [
    "CallIdPrefix",
    "CallViews",
    "FieldMarks",
    "FieldPlacement",
    "FieldView",
    "SchemaCall",
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
    PREFETCH = "prefetch-"

    def new_id(self) -> str:
        return f"{self.value}{uuid4().hex}"

    def marks(self, call_id: str | None) -> bool:
        """Идентификатор выдан этим источником, а не моделью."""
        if not call_id:
            return False

        return call_id.startswith(self.value)


class FieldPlacement(StrEnum):
    """Где показывается поле вызова: во входе шага, в его названии, нигде."""

    BODY = "body"
    HEADER = "header"
    HIDDEN = "hidden"


class FieldMarks:
    """Маркеры полей вызова по именам классов в metadata: сравнение типов
    между процессами невозможно."""

    INJECTED: ClassVar[frozenset[str]] = frozenset({"Injected"})
    """Injected фасада: им же хост помечает поле идентификатора вызова,
    дописанное в схему."""
    CONNECTION: ClassVar[frozenset[str]] = frozenset({"UserConnection"})
    NOT_LOGGED: ClassVar[frozenset[str]] = frozenset({"NotLogged"})
    """NotLogged фасада и его наследники: значение поля в лог не идёт."""

    @classmethod
    def injected(cls, field: FieldInfo) -> bool:
        return cls._marked(field.metadata, cls.INJECTED)

    @classmethod
    def connection(cls, field: FieldInfo) -> bool:
        return cls._marked(field.metadata, cls.CONNECTION)

    @classmethod
    def not_logged(cls, field: FieldInfo) -> bool:
        return cls._marked(field.metadata, cls.NOT_LOGGED)

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

    @staticmethod
    def _marked(metadata: Sequence[Any], markers: frozenset[str]) -> bool:
        for item in metadata:
            klass = item if isinstance(item, type) else type(item)
            names = {parent.__name__ for parent in klass.__mro__}
            if names & markers:
                return True

        return False


class ToolCallBase(BaseModel, ABC):
    """Вызов инструмента: аргументы протокола LLM как модель.

    Поля — аргументы тела, включая injected и порты: их отсеивают маркеры.
    Показы собираются из объявленных у полей результатов; наследник
    переопределяет их, когда нужен иной вид.
    """

    model_config = ConfigDict(frozen=True)

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

    def _argument_results(self) -> Iterator[ToolResultBase]:
        """Значение каждого присланного поля тела результатом по его объявлению;
        дефолты, которых модель не писала, во вход не идут."""
        for name, field in type(self).model_fields.items():
            if name not in self.model_fields_set:
                continue

            if self.placement_of(field, name) is not FieldPlacement.BODY:
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

    @staticmethod
    def placement_of(field: FieldInfo, name: str) -> FieldPlacement:
        """Injected и порты скрыты, intent в шапке, остальное в теле."""
        if FieldMarks.injected(field):
            return FieldPlacement.HIDDEN

        if FieldMarks.port(field):
            return FieldPlacement.HIDDEN

        if name == ToolIntent.NAME:
            return FieldPlacement.HEADER

        return FieldPlacement.BODY


class ToolCallModels:
    """Модели вызова по имени инструмента: наполняют фасад @tool (свои
    инструменты) и лента клиента (инструменты серверов, по виду из их
    карточек), читает лента.

    Повторная регистрация имени перезаписывает запись: тулы собираются на
    каждую сессию, и это контракт загрузки.
    """

    _MODELS: ClassVar[dict[str, type[ToolCallBase]]] = {}

    @classmethod
    def register(cls, tool_name: str, model: type[ToolCallBase]) -> None:
        cls._MODELS[tool_name] = model

    @classmethod
    def model_of(cls, tool_name: str) -> type[ToolCallBase] | None:
        """Модель вызова инструмента; None — у инструмента её нет."""
        return cls._MODELS.get(tool_name)

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


class FieldView(BaseModel):
    """Вид одного аргумента вызова в ленте: где он показывается и чем.

    display — объявленный показ значения: дамп результата (с kind), которым
    значение рисуется; None — обычная строка «имя: значение».
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    placement: FieldPlacement
    display: Mapping[str, Any] | None


class SchemaCall(ToolCallBase):
    """Вызов инструмента, вид аргументов которого пришёл вместе с его схемой.

    Инструмент исполняет сервер, и его кода в процессе клиента нет: вид
    аргументов приходит в карточке инструмента (ToolCard.views), и лента
    клиента строит из него наследника этого класса (CallViews.model_of).
    Показ тот же, что у вызова своего инструмента: скрытые поля не идут во
    вход шага, объявленный показ рисует значение своим результатом,
    остальные поля — строкой «имя: значение».
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    VIEWS: ClassVar[Mapping[str, FieldView]] = {}

    def chat_view(self) -> ChatView:
        blocks: list[str] = []
        for result in self._shown():
            blocks.append(result.chat_view().markdown)

        return ChatView(markdown="\n\n".join(blocks))

    def _shown(self) -> Iterator[ToolResultBase]:
        sent = self.model_extra
        if sent is None:
            return

        for name, value in sent.items():
            if value is None:
                continue

            if name == ToolIntent.NAME:
                continue

            view = self.VIEWS.get(name)
            if view is None:
                yield from self._line(name, value)
                continue

            if view.placement is not FieldPlacement.BODY:
                continue

            if view.display is None:
                yield from self._line(name, value)
                continue

            display = ToolArtifact.revive(view.display)
            if display is None:
                yield from self._line(name, value)
                continue

            yield display.bound(value)

    @staticmethod
    def _line(name: str, value: object) -> Iterator[ToolResultBase]:
        if isinstance(value, str) and not value:
            return

        yield MarkdownResult(text=FieldLines.line(name, value))


class CallViews:
    """Вид аргументов вызова: из модели вызова в карточку инструмента и в
    его схему на проводе.

    Сборка инструмента хоста зовёт of(): вид аргументов из модели вызова,
    которую построил фасад @tool. Сервер зовёт marked(): дописывает вид из
    карточки в схему инструмента. Клиент зовёт read(): читает метку схемы
    в карточку; схема без метки — инструмент чужого сервера, вида у него
    нет. Лента клиента зовёт model_of(): по виду из карточки строит модель
    вызова для показа входа шага.
    """

    MARK: ClassVar[str] = "x-boba-call-view"
    """Ключ схемы инструмента: вид аргументов по их именам."""

    def of(self, model: type[ToolCallBase]) -> Mapping[str, FieldView]:
        """Вид полей модели вызова, который отличается от обычной строки
        «имя: значение»."""
        views: dict[str, FieldView] = {}
        for name, field in model.model_fields.items():
            placement = model.placement_of(field, name)
            display = FieldMarks.display(field)
            dumped: dict[str, Any] | None = None
            if display is not None:
                dumped = display.model_dump(mode="json")

            if placement is FieldPlacement.BODY and dumped is None:
                continue

            views[name] = FieldView(placement=placement, display=dumped)

        return views

    def marked(
        self, schema: Mapping[str, Any], views: Mapping[str, FieldView] | None
    ) -> dict[str, Any]:
        """Схема с видом аргументов views; None — вида нет, схема как есть."""
        marked = dict(schema)
        if views is None:
            return marked

        dumped: dict[str, Any] = {}
        for name, view in views.items():
            dumped[name] = view.model_dump(mode="json")

        marked[self.MARK] = dumped

        return marked

    def read(self, schema: Mapping[str, Any]) -> Mapping[str, FieldView] | None:
        """Вид аргументов по метке схемы; None — метки нет.

        Ошибки:
        pydantic.ValidationError — метка есть, но вид поля не проходит модель.
        """
        raw = schema.get(self.MARK)
        if not isinstance(raw, Mapping):
            return None

        views: dict[str, FieldView] = {}
        for name, view in raw.items():
            views[str(name)] = FieldView.model_validate(view)

        return views

    def model_of(
        self, tool_name: str, views: Mapping[str, FieldView]
    ) -> type[SchemaCall]:
        """Модель вызова инструмента tool_name с видом аргументов views."""
        return type(f"{tool_name}_call", (SchemaCall,), {"VIEWS": dict(views)})


class RawCall(ToolCallBase):
    """Вызов инструмента без модели: аргументы показываются json-блоком."""

    args: Mapping[str, Any]

    def chat_view(self) -> ChatView:
        return MarkdownResult(
            text=JsonBlock.pretty(dict(self.args)), language="json"
        ).chat_view()
