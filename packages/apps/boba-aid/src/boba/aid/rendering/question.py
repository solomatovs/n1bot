"""Карточка вопроса агента пользователю — свойства элемента Question.

Панель висящего вопроса строится по форме `elicitation/create` агента,
сообщение вопроса в ленте — по вводу вызова, который агент пометил вопросом
пользователю (`_meta` MetaKey.QUESTION): `rawInput` читается моделью
AskedInput, итог вызова — результат tools/call в `rawOutput`, чей
structuredContent читается AskedOutput; обе модели — чатовая запись JSON
вопроса по договору с агентом (docs/developer-guide), ключи сверх них
отбрасываются. Запись вопросов сервера MCP — по раундам в результате вызова
(ElicitationRound). Поле формы — вопрос: варианты одиночного и
множественного выбора, логическое поле (варианты «да»/«нет» подписывает
элемент по переводам), строка и число — полем своего ответа; парное поле
«Other» своего вопроса агента вопросом не считается, его ключ — у поля
вопроса.

Ошибки: своих не выпускает; ввод и итог не по схеме дают запись без выбора.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, TypeAlias

import mcp_types as mt
from acp.experimental.v2 import schema
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.transport.acp.protocol import ElicitationRound, MetaKey

__all__ = [
    "AskedInput",
    "AskedOption",
    "AskedOutput",
    "AskedPick",
    "AskedQuestion",
    "CardAnswer",
    "CardItem",
    "CardOption",
    "CardSource",
    "QuestionCard",
    "QuestionCards",
    "QuestionKind",
    "QuestionState",
]

logger = logging.getLogger(__name__)

FormProperty: TypeAlias = (
    schema.ElicitationStringPropertySchema
    | schema.ElicitationNumberPropertySchema
    | schema.ElicitationIntegerPropertySchema
    | schema.ElicitationBooleanPropertySchema
    | schema.ElicitationMultiSelectPropertySchema
    | schema.ElicitationOtherPropertySchema
)
FormValue: TypeAlias = str | int | float | bool | list[str]


class QuestionState(StrEnum):
    """Состояние карточки: вопрос ждёт ответа либо исход ответа."""

    PENDING = "pending"
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class QuestionKind(StrEnum):
    """Вид вопроса: выбор вариантов либо поле своего ответа."""

    CHOICE = "choice"
    MULTI = "multi"
    BOOLEAN = "boolean"
    TEXT = "text"
    NUMBER = "number"
    INTEGER = "integer"


class AskedOption(BaseModel):
    """Вариант ответа в `rawInput` вопроса: метка, описание, предпросмотр."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str
    description: str = ""
    preview: str = ""


class AskedQuestion(BaseModel):
    """Вопрос в `rawInput`: текст (он же ключ ответа), метка вкладки,
    варианты и признак множественного выбора (`multiSelect`)."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    question: str
    header: str
    options: Sequence[AskedOption] = ()
    multi_select: bool = Field(default=False, alias="multiSelect")


class AskedInput(BaseModel):
    """`rawInput` вызова-вопроса: вопросы пользователю."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    questions: Sequence[AskedQuestion] = Field(min_length=1)


class AskedPick(BaseModel):
    """Выбор по одному вопросу в structuredContent итога: метки вариантов и
    свой текст «Other»; other None — свой текст не вводился."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    labels: Sequence[str] = ()
    other: str | None = None


class AskedOutput(BaseModel):
    """structuredContent итога вызова-вопроса: выбор по тексту вопроса и
    исход — accept, decline, cancel."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    picks: Mapping[str, AskedPick] = Field(default_factory=dict)
    outcome: Literal[QuestionState.ACCEPT, QuestionState.DECLINE, QuestionState.CANCEL]


class CardOption(BaseModel):
    """Вариант ответа: значение для формы, метка, описание и предпросмотр."""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str
    description: str = ""
    preview: str = ""


class CardItem(BaseModel):
    """Вопрос карточки: ключ поля формы, метка вкладки, текст, вид, варианты,
    ключ парного поля «Other» (пусто — его нет), обязательность и пределы
    числа."""

    model_config = ConfigDict(frozen=True)

    key: str
    header: str
    text: str
    kind: QuestionKind
    options: Sequence[CardOption] = ()
    other: str = ""
    required: bool
    minimum: float | None = None
    maximum: float | None = None


class CardAnswer(BaseModel):
    """Ответ на вопрос в записи: выбранные значения вариантов и свой текст;
    typed None — своего текста нет, пустая строка — выбран пустой «Other»."""

    model_config = ConfigDict(frozen=True)

    picked: Sequence[str] = ()
    typed: str | None = None


class CardSource(BaseModel):
    """Кто спрашивает: сервер MCP и его сообщение к форме."""

    model_config = ConfigDict(frozen=True)

    server: str
    message: str


class QuestionCard(BaseModel):
    """Свойства элемента Question: состояние, вопросы, источник (у вопроса
    сервера MCP), ответы записи по ключам вопросов и признак неудачи вызова
    (точка записи красная)."""

    model_config = ConfigDict(frozen=True)

    state: QuestionState
    items: Sequence[CardItem]
    source: CardSource | None = None
    answers: Mapping[str, CardAnswer] = Field(default_factory=dict)
    failed: bool = False

    def props(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class QuestionCards:
    """Строит карточки вопроса: висящий вопрос по форме агента (AgentFeed),
    запись ответа по вводу и итогу вызова-вопроса агента и по раундам
    вопросов сервера MCP (UpdateView)."""

    def pending(
        self, request: schema.CreateFormSessionElicitationRequest
    ) -> QuestionCard:
        """Висящий вопрос по форме; у вопроса сервера MCP источник — имя
        сервера из `_meta` и сообщение формы."""
        meta: Mapping[str, Any] = {}
        if request.field_meta is not None:
            meta = request.field_meta

        source: CardSource | None = None
        server = meta.get(MetaKey.SERVER.value)
        if isinstance(server, str):
            source = CardSource(server=server, message=request.message)

        items = list(self._items(request.requested_schema))

        return QuestionCard(state=QuestionState.PENDING, items=items, source=source)

    def asking(self, raw_input: Any) -> QuestionCard:
        """Вопрос агента, который ждёт ответа: вопросы — из ввода вызова."""
        return QuestionCard(
            state=QuestionState.PENDING, items=list(self._asked_items(raw_input))
        )

    def asked(self, raw_input: Any, raw_output: Any, failed: bool) -> QuestionCard:
        """Запись ответа на вопрос агента: вопросы — из ввода вызова, исход
        и выбор — из structuredContent итога; итог не результат tools/call с
        исходом вопроса (вызов оборван) — вопрос без ответа; failed — вызов
        кончился неудачей."""
        items = list(self._asked_items(raw_input))
        try:
            reply = mt.CallToolResult.model_validate(raw_output)
            output = AskedOutput.model_validate(reply.structured_content)
        except ValidationError:
            return QuestionCard(state=QuestionState.CANCEL, items=items, failed=failed)

        answers: dict[str, CardAnswer] = {}
        for text, pick in output.picks.items():
            answers[text] = CardAnswer(picked=pick.labels, typed=pick.other)

        return QuestionCard(
            state=output.outcome,
            items=items,
            answers=answers,
            failed=failed,
        )

    def _asked_items(self, raw_input: Any) -> Iterator[CardItem]:
        """Вопросы ввода вызова-вопроса; ввод не по схеме — без вопросов."""
        try:
            asked = AskedInput.model_validate(raw_input)
        except ValidationError as exc:
            logger.warning(
                "the question input of the agent is drawn without questions: %s", exc
            )
            return

        for question in asked.questions:
            yield self._asked_item(question)

    def round(self, asked: ElicitationRound) -> QuestionCard:
        """Запись ответа на вопрос сервера MCP по раунду вызова."""
        items = list(self._items(asked.requested))
        answers: dict[str, CardAnswer] = {}
        for item in items:
            value = asked.content.get(item.key)
            if value is None:
                continue

            answers[item.key] = self._answer(item, value)

        source = CardSource(server=asked.server, message=asked.message)

        return QuestionCard(
            state=QuestionState(asked.action),
            items=items,
            source=source,
            answers=answers,
            failed=asked.action != QuestionState.ACCEPT.value,
        )

    def _asked_item(self, question: AskedQuestion) -> CardItem:
        kind = QuestionKind.CHOICE
        if question.multi_select:
            kind = QuestionKind.MULTI

        options: list[CardOption] = []
        for option in question.options:
            options.append(
                CardOption(
                    value=option.label,
                    label=option.label,
                    description=option.description,
                    preview=option.preview,
                )
            )

        return CardItem(
            key=question.question,
            header=question.header,
            text=question.question,
            kind=kind,
            options=options,
            required=True,
        )

    def _items(self, requested: schema.ElicitationSchema) -> Iterator[CardItem]:
        """Вопросы формы по порядку полей; парные поля «Other» — не вопросы."""
        properties: Mapping[str, FormProperty] = {}
        if requested.properties is not None:
            properties = requested.properties

        required: set[str] = set()
        if requested.required is not None:
            required = set(requested.required)

        pairs: set[str] = set()
        for field in properties.values():
            if other := self._other_of(field):
                pairs.add(other)

        for key, field in properties.items():
            if key in pairs:
                continue

            item = self._item(key, field, key in required)
            if item is None:
                logger.warning(
                    "elicitation field %r of type %r has no view; it is not shown",
                    key,
                    field.type,
                )
                continue

            yield item

    def _item(self, key: str, field: FormProperty, required: bool) -> CardItem | None:
        """Вопрос по полю формы; None — у поля нет вида."""
        if isinstance(field, schema.ElicitationOtherPropertySchema):
            return None

        header = key
        if field.title:
            header = field.title

        text = header
        if field.description:
            text = field.description

        other = self._other_of(field)
        base = CardItem(
            key=key,
            header=header,
            text=text,
            kind=QuestionKind.TEXT,
            other=other,
            required=required or bool(other),
        )
        if isinstance(field, schema.ElicitationMultiSelectPropertySchema):
            return self._multi(base, field)

        if isinstance(field, schema.ElicitationBooleanPropertySchema):
            return base.model_copy(update={"kind": QuestionKind.BOOLEAN})

        if isinstance(field, schema.ElicitationNumberPropertySchema):
            return base.model_copy(
                update={
                    "kind": QuestionKind.NUMBER,
                    "minimum": field.minimum,
                    "maximum": field.maximum,
                }
            )

        if isinstance(field, schema.ElicitationIntegerPropertySchema):
            return base.model_copy(
                update={
                    "kind": QuestionKind.INTEGER,
                    "minimum": field.minimum,
                    "maximum": field.maximum,
                }
            )

        return self._single(base, field)

    def _single(
        self, base: CardItem, field: schema.ElicitationStringPropertySchema
    ) -> CardItem:
        """Строка: варианты `oneOf` либо `enum` — одиночный выбор, без них —
        поле своего ответа."""
        if field.one_of:
            options = list(self._titled(field.one_of))
            return base.model_copy(
                update={"kind": QuestionKind.CHOICE, "options": options}
            )

        if field.enum:
            options = list(self._untitled(field.enum))
            return base.model_copy(
                update={"kind": QuestionKind.CHOICE, "options": options}
            )

        return base

    def _multi(
        self, base: CardItem, field: schema.ElicitationMultiSelectPropertySchema
    ) -> CardItem | None:
        items = field.items
        if isinstance(items, schema.TitledMultiSelectItems):
            options = list(self._titled(items.any_of))
            return base.model_copy(
                update={"kind": QuestionKind.MULTI, "options": options}
            )

        if isinstance(items, schema.StringMultiSelectItems):
            options = list(self._untitled(items.enum))
            return base.model_copy(
                update={"kind": QuestionKind.MULTI, "options": options}
            )

        return None

    @staticmethod
    def _titled(options: Sequence[schema.EnumOption]) -> Iterator[CardOption]:
        for option in options:
            description = ""
            if option.description:
                description = option.description

            preview = ""
            if option.field_meta is not None:
                shown = option.field_meta.get(MetaKey.PREVIEW.value)
                if isinstance(shown, str):
                    preview = shown

            yield CardOption(
                value=option.const,
                label=option.title,
                description=description,
                preview=preview,
            )

    @staticmethod
    def _untitled(values: Sequence[str]) -> Iterator[CardOption]:
        for value in values:
            yield CardOption(value=value, label=value)

    @staticmethod
    def _other_of(field: FormProperty) -> str:
        """Ключ парного поля «Other» из `_meta` поля; пусто — пары нет."""
        if isinstance(field, schema.ElicitationOtherPropertySchema):
            return ""

        if field.field_meta is None:
            return ""

        other = field.field_meta.get(MetaKey.OTHER.value)
        if not isinstance(other, str):
            return ""

        return other

    @staticmethod
    def _answer(item: CardItem, value: FormValue) -> CardAnswer:
        """Ответ записи по значению поля: варианты — выбранными значениями,
        логическое — значением "true"/"false", строка и число — своим
        текстом."""
        if isinstance(value, list):
            return CardAnswer(picked=value)

        if isinstance(value, bool):
            picked = "false"
            if value:
                picked = "true"

            return CardAnswer(picked=[picked])

        if item.kind is QuestionKind.CHOICE:
            return CardAnswer(picked=[str(value)])

        return CardAnswer(typed=str(value))
