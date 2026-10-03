"""Открытое семейство результатов инструмента и его показы.

Тело инструмента возвращает модель-наследника ToolResultBase; всё, что нужно
остальным частям системы, семейство отдаёт методами базы: llm_view для
LLM, chat_view для ленты чата (markdown и элемент интерфейса). Экземпляр
результата без данных служит объявлением показа аргумента вызова: bound
кладёт значение аргумента на место данных. Наследник регистрируется
по kind при объявлении, и значение поля типа ToolResult восстанавливается из
JSON через реестр — перечисления вариантов нигде нет, вид результата может
объявить любой плагин.

Ошибки:
ResultTooLargeError — выдача больше потолка; текст готов для пользователя и LLM.
ResultKindError — вид результата объявлен с нарушением контракта семейства:
    kind без значения, kind занят другим классом, аннотация тела не
    результат.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from types import UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    Self,
    TypeAlias,
    Union,
    get_args,
    get_origin,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    SerializeAsAny,
    TypeAdapter,
    ValidatorFunctionWrapHandler,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, to_jsonable_python

__all__ = [
    "CanvasResult",
    "ChatElement",
    "ChatItem",
    "ChatView",
    "CodeBlock",
    "DetailBlock",
    "ErrorResult",
    "ExceptionResult",
    "Fact",
    "FactsBlock",
    "FailureCause",
    "FailureLayout",
    "FailureResult",
    "FailureResultField",
    "Fence",
    "FieldLines",
    "FileElement",
    "FileResult",
    "GridBlock",
    "JsonBlock",
    "MarkdownResult",
    "MarkdownTable",
    "NoteBlock",
    "NoteLine",
    "PanelOpen",
    "ResultKindError",
    "ResultKinds",
    "ResultTooLargeError",
    "ShellResult",
    "SqlFailureResult",
    "SqlResult",
    "SqlStatement",
    "TableResult",
    "TableText",
    "ToolArtifact",
    "ToolResult",
    "ToolResultBase",
    "VisualElement",
    "VisualResult",
]


class ResultTooLargeError(Exception):
    """Выдача больше потолка; сообщение готово для пользователя и LLM."""

    @classmethod
    def bytes_limit(cls, max_bytes: int) -> ResultTooLargeError:
        return cls(f"result exceeded {max_bytes} bytes; add LIMIT to the query")

    @classmethod
    def chars_limit(cls, max_chars: int) -> ResultTooLargeError:
        return cls(f"page content exceeded {max_chars} characters")


class ResultKindError(Exception):
    """Вид результата объявлен с нарушением контракта семейства."""


class Fence:
    """Ограда markdown-блока: одна на все места, где текст едет блоком."""

    BASE: ClassVar[str] = "```"

    @classmethod
    def around(cls, text: str, lang: str = "") -> str:
        """Блок с оградой длиннее любой внутри текста: она его не разорвёт."""
        fence = cls.BASE
        while fence in text:
            fence += "`"

        return f"{fence}{lang}\n{text}\n{fence}"


class JsonBlock:
    """Json-текст с отступами для показа: аргументы вызова и дампы моделей."""

    @staticmethod
    def pretty(payload: Any) -> str:
        """Чужие типы приводятся строкой: аргументы вызова приходят из
        протокола и сериализуемость не обещают."""
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


class FieldLines:
    """Строки именованных значений аргумента вызова по форме значения.

    Короткое значение — **имя:** `значение` в строку; многострочный или
    длинный текст — блоком; список объектов — таблицей; длинная структура —
    блоком json с отступами. Модели pydantic сначала
    приводятся к json-совместимому виду.
    """

    INLINE_CHARS: ClassVar[int] = 120
    """Самое длинное значение, которое ещё показывается в строку."""

    @classmethod
    def render(cls, fields: Mapping[str, Any]) -> Iterator[str]:
        for name, value in fields.items():
            yield cls.line(name, value)

    @classmethod
    def line(cls, name: str, value: Any) -> str:
        plain = to_jsonable_python(value, fallback=str)

        if isinstance(plain, str):
            return cls._text(name, plain)

        if rows := cls._rows(plain):
            return f"**{name}:**\n\n{TableText.render(rows)}"

        rendered = json.dumps(plain, ensure_ascii=False, default=str)
        if cls._inline(rendered):
            return f"**{name}:** `{rendered}`"

        return f"**{name}:**\n{Fence.around(JsonBlock.pretty(plain), 'json')}"

    @classmethod
    def _text(cls, name: str, text: str) -> str:
        if cls._inline(text):
            return f"**{name}:** `{text}`"

        return f"**{name}:**\n{Fence.around(text)}"

    @classmethod
    def _inline(cls, text: str) -> bool:
        if "\n" in text:
            return False

        if "`" in text:
            return False

        return len(text) <= cls.INLINE_CHARS

    @staticmethod
    def _rows(value: Any) -> list[Mapping[str, Any]]:
        """Строки таблицы из списка объектов; пусто — значение не таблица."""
        if not isinstance(value, list):
            return []

        rows: list[Mapping[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                return []

            rows.append(item)

        return rows


class NoteLine:
    """Служебная строка курсивом: статус, усечение, подпись вместо элемента."""

    @staticmethod
    def render(text: str) -> str:
        return f"_{text}_"


class MarkdownTable:
    """Таблица GitHub-markdown из строк с общими ключами: шапка из ключей
    первой строки, колонки выровнены по самой широкой ячейке."""

    PAD: ClassVar[int] = 2
    """Минимальная ширина колонки под разделитель `--`."""

    @classmethod
    def render(cls, rows: Sequence[Mapping[str, str]]) -> str:
        headers = list(cls._headers(rows))

        widths: dict[str, int] = {}
        for header in headers:
            widths[header] = max(cls.PAD, len(header))

        for row in rows:
            for header in headers:
                cell = row.get(header, "")
                widths[header] = max(widths[header], len(cell))

        lines: list[str] = []
        lines.append(cls._line(headers, headers, widths))
        lines.append(cls._separator(headers, widths))
        for row in rows:
            cells: list[str] = []
            for header in headers:
                cells.append(row.get(header, ""))

            lines.append(cls._line(headers, cells, widths))

        return "\n".join(lines)

    @staticmethod
    def _headers(rows: Sequence[Mapping[str, str]]) -> Iterator[str]:
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key in seen:
                    continue

                seen.add(key)
                yield key

    @staticmethod
    def _line(
        headers: Sequence[str], cells: Sequence[str], widths: Mapping[str, int]
    ) -> str:
        padded: list[str] = []
        for header, cell in zip(headers, cells, strict=True):
            padded.append(cell.ljust(widths[header]))

        return "| " + " | ".join(padded) + " |"

    @staticmethod
    def _separator(headers: Sequence[str], widths: Mapping[str, int]) -> str:
        dashes: list[str] = []
        for header in headers:
            dashes.append("-" * (widths[header] + 2))

        return "|" + "|".join(dashes) + "|"


class ChatElement(StrEnum):
    """Имена элементов ленты, которые результаты семейства зовут по себе.

    PLOTLY — встроенный элемент chainlit с фигурой из props; остальные —
    jsx-компоненты public/elements/<имя>.jsx.
    """

    PLOTLY = "plotly"
    CANVAS_VIEW = "CanvasView"


class VisualElement(BaseModel):
    """Виджет рядом с шагом: график plotly по имени ChatElement.PLOTLY либо
    jsx-компонент public/elements/<element>.jsx с props."""

    model_config = ConfigDict(frozen=True)

    item: Literal["visual"] = "visual"
    element: str
    props: Mapping[str, Any]
    title: str = ""


class FileElement(BaseModel):
    """Вложение: файл workspace, который чат отдаёт пользователю ссылкой."""

    model_config = ConfigDict(frozen=True)

    item: Literal["file"] = "file"
    path: str
    name: str
    mime: str


class PanelOpen(BaseModel):
    """Команда поверхности: открыть файл workspace в панели канваса и оставить
    ссылку на него в переписке. Показ и есть проверка файла: если вьювер
    браузера отвечает вердиктом и не смог показать файл, вызов считается
    неудачным."""

    model_config = ConfigDict(frozen=True)

    item: Literal["panel"] = "panel"
    path: str


ChatItem: TypeAlias = Annotated[
    VisualElement | FileElement | PanelOpen,
    Field(discriminator="item"),
]
"""Словарь того, что лента умеет смонтировать помимо markdown: закрыт
намеренно; результаты собирают показ из него."""


class ChatView(BaseModel):
    """Показ результата в ленте чата: markdown выхода шага и элементы,
    которые поверхность чата монтирует рядом с шагом."""

    model_config = ConfigDict(frozen=True)

    markdown: str
    items: Sequence[ChatItem] = ()


class Fact(BaseModel):
    """Пара «ключ: значение» списка фактов.

    Длинное и многострочное значение в markdown едет блоком: инлайн-код его
    не удержит, а у текста вроде стрелки `^` под строкой SQL сломалось бы
    выравнивание.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    value: str

    INLINE_CHARS: ClassVar[int] = 120
    """Самое длинное значение, которое ещё показывается в строку."""

    def markdown(self) -> str:
        if self._blocky():
            return f"**{self.key}:**\n{Fence.around(self.value)}"

        return f"**{self.key}:** `{self.value}`"

    def plain(self) -> str:
        return f"{self.key}: {self.value}"

    def _blocky(self) -> bool:
        if "\n" in self.value:
            return True

        if "`" in self.value:
            return True

        return len(self.value) > self.INLINE_CHARS


class CodeBlock(BaseModel):
    """Текст или код; language — подсветка, пусто — простой текст."""

    model_config = ConfigDict(frozen=True)

    block: Literal["code"] = "code"
    text: str
    language: str = ""

    def markdown(self) -> str:
        return Fence.around(self.text, self.language)

    def plain(self) -> str:
        return self.text


class GridBlock(BaseModel):
    """Сетка строк с общими ключами."""

    model_config = ConfigDict(frozen=True)

    block: Literal["grid"] = "grid"
    rows: Sequence[Mapping[str, Any]]

    def markdown(self) -> str:
        return MarkdownTable.render(list(self._cells()))

    def plain(self) -> str:
        return self.markdown()

    def _cells(self) -> Iterator[dict[str, str]]:
        for row in self.rows:
            cells: dict[str, str] = {}
            for key, value in row.items():
                cells[key] = str(value)

            yield cells


class FactsBlock(BaseModel):
    """Список фактов: код возврата, движок, статус."""

    model_config = ConfigDict(frozen=True)

    block: Literal["facts"] = "facts"
    facts: Sequence[Fact]

    def markdown(self) -> str:
        lines: list[str] = []
        for fact in self.facts:
            lines.append(fact.markdown())

        return "\n\n".join(lines)

    def plain(self) -> str:
        lines: list[str] = []
        for fact in self.facts:
            lines.append(fact.plain())

        return "\n".join(lines)


class NoteBlock(BaseModel):
    """Подпись: усечение, статус команды, диагностика."""

    model_config = ConfigDict(frozen=True)

    block: Literal["note"] = "note"
    text: str

    def markdown(self) -> str:
        return NoteLine.render(self.text)

    def plain(self) -> str:
        return self.text


DetailBlock: TypeAlias = Annotated[
    CodeBlock | GridBlock | FactsBlock | NoteBlock,
    Field(discriminator="block"),
]
"""Словарь блоков, из которых ошибка собирает свои детали: закрыт намеренно,
как синтаксис markdown для чата."""


class ResultKinds:
    """Реестр видов результата: kind -> класс; наполняется при объявлении
    наследника ToolResultBase, читается при восстановлении из JSON и при
    разборе аннотации тела инструмента."""

    _KINDS: ClassVar[dict[str, type[ToolResultBase]]] = {}

    @classmethod
    def add(cls, kind: str, model: type[ToolResultBase]) -> None:
        """Ошибки:
        ResultKindError — kind уже занят другим классом.
        """
        known = cls._KINDS.get(kind)
        if known is not None and known is not model:
            msg = (
                f"result kind {kind!r} of {model.__qualname__} is already "
                f"registered by {known.__qualname__}"
            )
            raise ResultKindError(msg)

        cls._KINDS[kind] = model

    @classmethod
    def of(cls, kind: str) -> type[ToolResultBase] | None:
        return cls._KINDS.get(kind)

    @classmethod
    def kinds(cls) -> frozenset[str]:
        return frozenset(cls._KINDS)

    @classmethod
    def models_of(cls, annotation: Any) -> tuple[type[ToolResultBase], ...]:
        """Классы результата из аннотации возврата тела: класс либо union.

        Сама база — допустимая аннотация: тело вправе вернуть любой результат.

        Ошибки:
        ResultKindError — аннотация не результат и не union результатов.
        """
        members = list(cls._union_members(annotation))

        models: list[type[ToolResultBase]] = []
        for member in members:
            if not isinstance(member, type):
                msg = f"return annotation {annotation!r} is not a tool result"
                raise ResultKindError(msg)

            if not issubclass(member, ToolResultBase):
                msg = (
                    f"return annotation {annotation!r}: {member.__qualname__} "
                    "is not a ToolResultBase subclass"
                )
                raise ResultKindError(msg)

            models.append(member)

        return tuple(models)

    @classmethod
    def kinds_of(cls, annotation: Any) -> tuple[str, ...]:
        """Виды результата из аннотации возврата тела; база видов не называет.

        Ошибки:
        ResultKindError — аннотация не результат и не union результатов.
        """
        kinds: list[str] = []
        for model in cls.models_of(annotation):
            kind = model.declared_kind()
            if kind is None:
                continue

            kinds.append(kind)

        return tuple(kinds)

    @staticmethod
    def _union_members(annotation: Any) -> Iterator[Any]:
        origin = get_origin(annotation)
        if origin is Union or origin is UnionType:
            yield from get_args(annotation)
            return

        yield annotation


class ToolResultBase(BaseModel, ABC):
    """База открытого семейства результатов инструмента.

    Наследник задаёт kind литералом с дефолтом и реализует llm_view (текст
    для LLM) и chat_view (показ в ленте: markdown и элемент).
    Объявление наследника регистрирует его в ResultKinds по kind, а
    валидация значения как ToolResultBase находит класс по kind в JSON — так
    поле artifact конверта и items набора восстанавливаются без перечисления
    вариантов.

    ok — единственный признак успеха: ненулевой код выхода или ошибка сервера
    обязаны выставить ok=False.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    ok: bool = True

    elapsed_ms: int = 0
    """Время вызова целиком, его проставляет обвязка запуска; 0 — не измерено.

    Лежит в результате, а не в шаге ленты: артефакт переживает перезагрузку
    вкладки, и сборка ленты из истории показывает то же время, что и live.
    """
    metadata: Mapping[str, str] = Field(default_factory=dict)
    """Служебные пометки владельца результата: версия снимка, имя задачи."""

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        kind = cls.declared_kind()
        if kind is None:
            return

        ResultKinds.add(kind, cls)

    @classmethod
    def declared_kind(cls) -> str | None:
        """kind наследника; None у базы и промежуточных классов без дефолта."""
        field = cls.model_fields["kind"]
        if field.is_required():
            return None

        return str(field.default)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        """Экземпляр в Annotated поля вызова — объявление показа, не тип:
        схема поля остаётся схемой его значения."""
        if source is cls:
            return super().__get_pydantic_core_schema__(source, handler)

        return handler(source)

    _SCHEMA_DEPTH: ClassVar[int] = 0
    """Глубина сборки JSON-схемы базы: вложенный вызов отдаёт саму базу."""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """JSON-схема базы — oneOf зарегистрированных видов: OpenAPI видит
        поля каждого, а поле вложенного набора остаётся базой."""
        if cls is not ToolResultBase:
            return handler(core_schema)

        if ToolResultBase._SCHEMA_DEPTH > 0:
            return handler(core_schema)

        ToolResultBase._SCHEMA_DEPTH += 1
        try:
            variants: list[JsonSchemaValue] = []
            for kind in sorted(ResultKinds.kinds()):
                model = ResultKinds.of(kind)
                if model is None:
                    continue

                variants.append(handler(model.__pydantic_core_schema__))
        finally:
            ToolResultBase._SCHEMA_DEPTH -= 1

        return {"oneOf": variants, "discriminator": {"propertyName": "kind"}}

    @model_validator(mode="wrap")
    @classmethod
    def _by_kind(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Значение, валидируемое как база или промежуточный класс без kind
        (FailureResult), уходит классу своего kind."""
        if cls.declared_kind() is not None:
            return handler(value)

        if isinstance(value, cls):
            return value

        if not isinstance(value, Mapping):
            return handler(value)

        kind = value.get("kind")
        model = ResultKinds.of(str(kind))
        if model is None:
            msg = f"unknown tool result kind {kind!r}"
            raise ValueError(msg)

        if not issubclass(model, cls):
            msg = f"tool result kind {kind!r} is not a {cls.__name__}"
            raise ValueError(msg)

        return model.model_validate(value)

    @abstractmethod
    def llm_view(self) -> str:
        """Текст результата для LLM: он же content конверта вызова."""

    @abstractmethod
    def chat_view(self) -> ChatView:
        """Показ результата в ленте чата: markdown шага и элемент."""

    def bound(self, value: Any) -> Self:
        """Копия с значением аргумента вызова на месте данных: показ аргумента
        этим классом. Результат без такого места аргументом не объявляется.

        Ошибки:
        ResultKindError — класс не принимает значение аргумента.
        """
        msg = f"{type(self).__name__} cannot display a call argument"
        raise ResultKindError(msg)

    def packed(self) -> tuple[str, Self]:
        """Пара (content, artifact) для langchain-инструмента."""
        return self.llm_view(), self


ToolResult: TypeAlias = SerializeAsAny[ToolResultBase]
"""Любой результат семейства: поле такого типа восстанавливается по kind и
сериализуется полями фактического класса."""


class MarkdownResult(ToolResultBase):
    """Текст в markdown: результат без структурированного payload'а.

    Экземпляр без текста — объявление показа аргумента вызова: тот же
    язык блока получит значение аргумента.
    """

    kind: Literal["markdown"] = "markdown"
    text: str = ""
    language: str = ""
    """Язык блока показа: непустой уводит текст в блок ```<language>
    (дамп csv, лог, вывод чужого формата); пусто — текст уже markdown."""
    note: str | None = None
    """Footer-контекст под текстом: источник, окно строк, усечение."""

    def llm_view(self) -> str:
        if self.note is None:
            return self.text

        if not self.text.strip():
            return self.note

        return f"{self.text}\n\n{self.note}"

    def chat_view(self) -> ChatView:
        """Текст как есть либо блоком с языком; пустой текст оставляет одну подпись."""
        if not self.text.strip():
            if self.note is None:
                return ChatView(markdown="")

            return ChatView(markdown=NoteLine.render(self.note))

        body = self.text
        if self.language:
            body = Fence.around(self.text.strip("\n"), self.language)

        if self.note is None:
            return ChatView(markdown=body)

        return ChatView(markdown=f"{body}\n\n{NoteLine.render(self.note)}")

    def bound(self, value: Any) -> Self:
        return self.model_copy(update={"text": str(value).strip("\n")})


class TableText:
    """Строки выборки как markdown-таблица: ячейки приводятся к строкам,
    переносы заменяются меткой — таблица markdown многострочных ячеек не держит."""

    NEWLINE_MARK: ClassVar[str] = " ⏎ "

    @classmethod
    def render(cls, rows: Sequence[Mapping[str, Any]]) -> str:
        if not rows:
            return NoteLine.render("(no rows)")

        flat = list(cls._flat_rows(rows))

        return MarkdownTable.render(flat)

    @classmethod
    def _flat_rows(cls, rows: Sequence[Mapping[str, Any]]) -> Iterator[dict[str, str]]:
        for row in rows:
            flat: dict[str, str] = {}
            for key, value in row.items():
                flat[key] = cls._cell(value)

            yield flat

    @classmethod
    def _cell(cls, value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, str):
            return cls._flatten(value)

        if isinstance(value, (list, tuple, dict)):
            return cls._flatten(json.dumps(value, ensure_ascii=False))

        return cls._flatten(str(value))

    @classmethod
    def _flatten(cls, cell: str) -> str:
        mark = cls.NEWLINE_MARK

        return cell.replace("\r\n", mark).replace("\n", mark).replace("\r", mark)


class TableResult(ToolResultBase):
    """Таблица не из SQL: строки-записи с общими ключами (поиск, оглавление,
    каталог соединений)."""

    kind: Literal["table"] = "table"
    rows: Sequence[Mapping[str, Any]] = ()
    note: str | None = None
    """Footer-контекст под таблицей: усечение, предупреждения и т.п."""

    def llm_view(self) -> str:
        body = json.dumps(self.rows, ensure_ascii=False)
        if self.note is None:
            return body

        return f"{body}\n\n{self.note}"

    def chat_view(self) -> ChatView:
        body = TableText.render(self.rows)
        if self.note:
            return ChatView(markdown=f"\n{body}\n\n{NoteLine.render(self.note)}")

        return ChatView(markdown=f"\n{body}")

    def bound(self, value: Any) -> Self:
        return self.model_copy(update={"rows": list(value)})


class SqlStatement(BaseModel):
    """Итог одной команды запроса; драйвер заполняет то, что умеет отдать.

    Команда с текстом или фактами — отчёт и показывается целиком: блок SQL,
    подпись, строки и факты — так насосы показывают, что именно выполнили.
    Остальное — итог выборки пользователя: подпись у неё только там, где
    строк нет или команд несколько.
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    """Выполненный текст команды; пусто — его показывать незачем."""
    rows: Sequence[Mapping[str, Any]] | None = None
    """Выборка; None — команда без результирующего набора (DML, DDL)."""
    affected_rows: int | None = None
    """Счётчик затронутых строк, если драйвер его даёт."""
    status: str = ""
    """Статус сервера как есть ('UPDATE 5' у postgres); пусто — сервер не отдаёт."""
    facts: Sequence[Fact] = Field(default_factory=list)
    """Что сервер сообщил помимо строк: сессия, счётчики, notices."""
    note: str = ""
    """Усечение или окно листания: 'truncated to max_rows (500)',
    'rows 51-100; more rows available, next offset=100'."""

    def caption(self) -> str:
        """Подпись команды: статус сервера, иначе вывод из фактов."""
        if self.status:
            return self.status

        if self.rows is not None:
            return f"{len(self.rows)} rows"

        if self.affected_rows is not None:
            return f"affected rows: {self.affected_rows}"

        return "statement executed"

    def llm_text(self, captioned: bool) -> str:
        """Подпись строкой над телом: текст команды, факты, строки JSON'ом и note."""
        body = self._llm_body()
        if not self._captioned(captioned):
            return body

        if not body:
            return self.caption()

        return f"{self.caption()}\n{body}"

    def _llm_body(self) -> str:
        parts: list[str] = []
        if self.text:
            parts.append(f"statement: {self.text}")

        if self.facts:
            parts.append(FactsBlock(facts=self.facts).plain())

        if self.rows is not None:
            parts.append(json.dumps(self.rows, ensure_ascii=False, default=str))

        if self.note:
            parts.append(self.note)

        return "\n\n".join(parts)

    def markdown(self, captioned: bool) -> str:
        """Блок SQL, подпись курсивом, факты, таблица и note под ними."""
        parts: list[str] = []
        if self.text:
            parts.append(Fence.around(self.text, "sql"))

        if self._captioned(captioned):
            parts.append(NoteLine.render(self.caption()))

        if self.facts:
            parts.append(FactsBlock(facts=self.facts).markdown())

        if self.rows is not None:
            parts.append(TableText.render(self.rows))

        if self.note:
            parts.append(NoteLine.render(self.note))

        return "\n\n".join(parts)

    def _captioned(self, captioned: bool) -> bool:
        """Подпись нужна среди нескольких команд, у команды без строк и у
        отчёта — команды с текстом или фактами."""
        if captioned:
            return True

        if self.rows is None:
            return True

        if self.text:
            return True

        return bool(self.facts)


class SqlResult(ToolResultBase):
    """Итог SQL-запроса к любой базе: команды одного запроса по порядку.

    Одна команда показывается как есть; у нескольких перед каждой стоит её
    подпись — статус сервера либо число строк. Насосы кладут сюда же шаги
    before/after и свою команду с текстом, контрактом колонок и фактами
    сервера.
    """

    kind: Literal["sql"] = "sql"
    engine: str
    """Движок базы: 'postgres', 'clickhouse'; подпись для фронта."""
    statements: Sequence[SqlStatement]

    def llm_view(self) -> str:
        captioned = len(self.statements) > 1

        parts: list[str] = []
        for statement in self.statements:
            parts.append(statement.llm_text(captioned))

        return "\n\n".join(parts)

    def chat_view(self) -> ChatView:
        if len(self.statements) == 1:
            return ChatView(markdown=f"\n{self.statements[0].markdown(False)}")

        blocks: list[str] = []
        for statement in self.statements:
            blocks.append(statement.markdown(True))

        return ChatView(markdown="\n\n".join(blocks))


class VisualResult(ToolResultBase):
    """Визуальный виджет ленты: имя элемента и его props.

    Имя из ChatElement.PLOTLY — встроенный график chainlit с figure spec в
    props; любое другое — jsx-компонент public/elements/<element>.jsx.
    """

    kind: Literal["visual"] = "visual"
    element: str
    props: Mapping[str, Any]
    title: str | None = None
    """Человекочитаемый заголовок — для сводки в LLM и подписи в UI."""
    summary: str = ""
    """Текст для LLM вместо пометки о показе: что сделано и что дальше."""

    PLOTLY_SPEC: ClassVar[str] = "spec"
    """Ключ props графика, под которым едет figure spec."""

    @classmethod
    def plotly(cls, spec: Mapping[str, Any], title: str | None) -> VisualResult:
        """График plotly: элемент встроенный, spec — figure как dict."""
        return cls(
            element=ChatElement.PLOTLY, props={cls.PLOTLY_SPEC: spec}, title=title
        )

    def llm_view(self) -> str:
        if self.summary:
            return self.summary

        if self.title:
            return f"[{self.element} rendered: {self.title}]"

        return f"[{self.element} rendered]"

    def chat_view(self) -> ChatView:
        """Подпись вместо самого элемента: элемент уходит рядом с шагом."""
        caption = NoteLine.render(f"({self.element})")
        title = ""
        if self.title:
            caption = NoteLine.render(f"({self.element}: {self.title})")
            title = self.title

        widget = VisualElement(element=self.element, props=self.props, title=title)

        return ChatView(markdown=caption, items=[widget])

    def bound(self, value: Any) -> Self:
        return self.model_copy(update={"props": dict(value)})


class FileResult(ToolResultBase):
    """Файл workspace, отданный пользователю вложением в чат."""

    kind: Literal["file"] = "file"
    path: str
    name: str
    mime: str

    def llm_view(self) -> str:
        return f"file attached to the chat: {self.name}"

    def chat_view(self) -> ChatView:
        attachment = FileElement(path=self.path, name=self.name, mime=self.mime)

        return ChatView(markdown=self.llm_view(), items=[attachment])


class CanvasResult(ToolResultBase):
    """Файл workspace, показанный в панели канваса: путь, подпись и что
    сказать модели дальше."""

    kind: Literal["canvas"] = "canvas"
    path: str
    label: str
    summary: str = ""
    """Что сделано, словами для LLM; пусто — стандартная фраза о показе."""
    note: str = ""
    """Оговорка для LLM: что происходит дальше и как читать отказ."""

    def llm_view(self) -> str:
        text = self.summary
        if not text:
            text = f"opened in the canvas: {self.label} ({self.path})"

        if self.note:
            text = f"{text}; {self.note}"

        return text

    def chat_view(self) -> ChatView:
        return ChatView(markdown=self.llm_view(), items=[PanelOpen(path=self.path)])


class ShellResult(ToolResultBase):
    """Итог shell-команды: код возврата и её потоки вывода.

    Сама команда здесь не хранится: её рисует вход шага из аргументов
    вызова. Потоки хранятся врозь: LLM разбирает их по отдельности, а
    пользователю показывается один — stdout, а когда он пуст, stderr.
    """

    kind: Literal["shell"] = "shell"
    exit_code: int
    stdout: str
    stdout_bytes: int
    """Полный размер stdout до усечения."""
    stdout_truncated: bool
    stderr: str
    stderr_bytes: int
    """Полный размер stderr до усечения."""
    stderr_truncated: bool
    duration_ms: int
    timed_out: bool

    LLM_SKIP: ClassVar[frozenset[str]] = frozenset({"kind", "ok", "metadata"})
    """Поля конверта: в отчёте для LLM им места нет."""

    @property
    def shows_stdout(self) -> bool:
        """Показывается stdout; пустой stdout уступает место stderr."""
        return bool(self.stdout.strip())

    @property
    def output(self) -> str:
        """Поток, который видит пользователь."""
        if self.shows_stdout:
            return self.stdout

        return self.stderr

    @property
    def truncated(self) -> bool:
        """Усечён ли показанный поток."""
        if self.shows_stdout:
            return self.stdout_truncated

        return self.stderr_truncated

    def llm_view(self) -> str:
        report = self.model_dump(exclude=set(self.LLM_SKIP))

        return json.dumps(report, ensure_ascii=False)

    def chat_view(self) -> ChatView:
        """Вывод команды блоком, под ним код возврата обычной строкой.

        Шапка блока называет показанный поток: chainlit берёт её из ограды
        регуляркой language-(\\w+), человеческий текст туда не проходит —
        поэтому код возврата живёт строкой под блоком.
        """
        blocks: list[str] = []

        output = self.output.strip("\n")
        if output:
            heading = "stdout"
            if not self.shows_stdout:
                heading = "stderr"
            blocks.append(Fence.around(output, heading))
        else:
            blocks.append(NoteLine.render("(no output)"))

        blocks.append(self._note())

        return ChatView(markdown="\n\n".join(blocks))

    def _note(self) -> str:
        """Итог выполнения одной строкой: код возврата всегда, помехи следом.

        Отрицательный код возврата — процесс убит сигналом: так его отдаёт
        python, и «exit code: -9» читался бы как ошибка рендера.
        """
        notes: list[str] = []

        if self.exit_code < 0:
            notes.append(f"killed by signal {-self.exit_code}")
        else:
            notes.append(f"exit code: {self.exit_code}")

        if self.timed_out:
            notes.append("timed out")

        if self.truncated:
            notes.append("output truncated")

        return NoteLine.render("; ".join(notes))


class FailureLayout:
    """Раскладка ошибки по каналам: markdown ленты и плоский текст LLM и журнала.

    Ошибка описывает себя заголовком и блоками словаря DetailBlock; блок сам
    знает свой markdown и плоский текст, а раскладка ставит над ними
    заголовок — поэтому любая ошибка семейства выглядит одинаково во всех
    каналах.
    """

    MARKDOWN_SPECIALS: ClassVar[str] = "\\`*_[]<>|~"

    def markdown(self, headline: str, blocks: Sequence[DetailBlock]) -> str:
        parts: list[str] = [f"**{self._escaped(headline)}**"]
        for block in blocks:
            parts.append(block.markdown())

        return "\n\n".join(parts)

    def plain(self, headline: str, blocks: Sequence[DetailBlock]) -> str:
        parts: list[str] = [headline]
        for block in blocks:
            parts.append(block.plain())

        return "\n".join(parts)

    def _escaped(self, text: str) -> str:
        escaped: list[str] = []
        for char in text:
            if char in self.MARKDOWN_SPECIALS:
                escaped.append("\\")

            escaped.append(char)

        return "".join(escaped)


class FailureResult(ToolResultBase):
    """База ошибок открытого семейства результатов: единая спецификация показа.

    Ошибка — такой же результат, как таблица или SQL: едет в конверте
    инструмента, ложится в историю LLM артефактом и поднимается из неё по
    kind. Наследник описывает ошибку данными — заголовком в одну строку
    (что сломалось), деталями из словаря блоков DetailBlock, которые видят все
    каналы, и трассой для журнала, — а текст для ленты, LLM и
    журнала собирает раскладка FailureLayout. Любое исключение
    превращает в наследника упаковщик FailurePacker (boba.toolkit.failure).
    """

    ok: bool = False
    error_kind: str
    """Машинный класс ошибки: имя типа исключения либо код отказа."""

    _LAYOUT: ClassVar[FailureLayout] = FailureLayout()

    @abstractmethod
    def headline(self) -> str:
        """Одна строка: что сломалось."""

    @abstractmethod
    def details(self) -> Sequence[DetailBlock]:
        """Подробности для всех каналов: текст ошибки, причины, место."""

    def trace(self) -> str:
        """Трасса стека для журнала; пусто — трассы нет."""
        return ""

    def llm_view(self) -> str:
        return self._LAYOUT.plain(self.headline(), self.details())

    def chat_view(self) -> ChatView:
        return ChatView(markdown=self._LAYOUT.markdown(self.headline(), self.details()))

    def log_view(self) -> str:
        """Текст для журнала: то же, что видит LLM, плюс трасса."""
        text = self.llm_view()

        trace = self.trace()
        if not trace:
            return text

        return f"{text}\n{trace}"


FailureResultField: TypeAlias = SerializeAsAny[FailureResult]
"""Любая ошибка семейства: восстанавливается по kind, как ToolResult."""


class ErrorResult(FailureResult):
    """Ошибка, чей текст уже написан для человека и LLM: отказ, сбой обвязки.

    Первая строка текста — заголовок, остальные идут блоком как есть.
    """

    kind: Literal["error"] = "error"
    message: str

    def headline(self) -> str:
        first, _, _ = self.message.partition("\n")

        return first

    def details(self) -> Sequence[DetailBlock]:
        _, _, rest = self.message.partition("\n")
        if not rest.strip():
            return ()

        return (CodeBlock(text=rest),)


class FailureCause(BaseModel):
    """Звено цепочки `raise ... from` упакованного исключения."""

    model_config = ConfigDict(frozen=True)

    error_type: str
    message: str


class ExceptionResult(FailureResult):
    """Исключение, упакованное без собственного вида ошибки.

    Заголовок — тип и первая строка текста; остальной текст, место
    возникновения и цепочка причин идут подробностями, полная трасса —
    только в журнал. Причина, чей текст уже показан выше по
    цепочке, называется одним типом: драйверы и обёртки часто повторяют
    одно и то же сообщение.
    """

    kind: Literal["exception"] = "exception"
    message: str
    causes: Sequence[FailureCause] = ()
    raised_at: str = ""
    traceback: str = ""

    HEADLINE_CHARS: ClassVar[int] = 160

    def headline(self) -> str:
        first, _, _ = self.message.strip().partition("\n")
        if not first:
            return self.error_kind

        return f"{self.error_kind}: {self._clipped(first)}"

    def details(self) -> Sequence[DetailBlock]:
        blocks: list[DetailBlock] = []

        if body := self._body():
            blocks.append(CodeBlock(text=body))

        blocks.extend(self._facts())

        return blocks

    def llm_view(self) -> str:
        """Текст целиком одной записью: заголовок ленты режется, а модели и
        журналу нужна вся строка без повтора блоком."""
        text = self.message.strip()
        if not text:
            return self._LAYOUT.plain(self.error_kind, self._facts())

        return self._LAYOUT.plain(f"{self.error_kind}: {text}", self._facts())

    def trace(self) -> str:
        return self.traceback

    def _clipped(self, line: str) -> str:
        """Строка заголовка не длиннее потолка, обрезанная по слову."""
        if len(line) <= self.HEADLINE_CHARS:
            return line

        head = line[: self.HEADLINE_CHARS]
        cut = head.rfind(" ")
        if cut > 0:
            head = head[:cut]

        return head + " …"

    def _body(self) -> str:
        """Текст сверх заголовка: весь текст, если первая строка не влезла."""
        first, _, rest = self.message.strip().partition("\n")
        if len(first) > self.HEADLINE_CHARS:
            return self.message.strip()

        return rest

    def _facts(self) -> list[DetailBlock]:
        facts: list[Fact] = []
        if self.raised_at:
            facts.append(Fact(key="raised at", value=self.raised_at))

        facts.extend(self._cause_facts())

        if not facts:
            return []

        return [FactsBlock(facts=facts)]

    def _cause_facts(self) -> Iterator[Fact]:
        shown = [self.message]
        for cause in self.causes:
            if self._repeats(cause.message, shown):
                yield Fact(key="caused by", value=f"{cause.error_type} (same message)")
                continue

            shown.append(cause.message)

            if not cause.message:
                yield Fact(key="caused by", value=cause.error_type)
                continue

            yield Fact(key="caused by", value=f"{cause.error_type}: {cause.message}")

    def _repeats(self, message: str, shown: Sequence[str]) -> bool:
        if not message:
            return False

        containing = self._containing(message, shown)

        return any(containing)

    @staticmethod
    def _containing(message: str, shown: Sequence[str]) -> Iterator[bool]:
        for text in shown:
            yield message in text


class SqlFailureResult(FailureResult):
    """Сбой вызова, который выполнял SQL: ошибка, колонки, с которыми шла
    работа, и все команды по порядку, упавшая — последней.

    При падении эти подробности нужнее всего: по ним видно, какой запрос
    сгенерирован и на чём сервер отказал. Собирает его журнал команд вызова
    (boba.toolkit.transfer.CommandJournal) по исключению тела насоса.
    """

    kind: Literal["sql_failure"] = "sql_failure"
    cause: FailureResultField
    columns: Sequence[Mapping[str, str]] = ()
    statements: Sequence[SqlStatement] = ()

    def headline(self) -> str:
        return self.cause.headline()

    def details(self) -> Sequence[DetailBlock]:
        blocks: list[DetailBlock] = list(self.cause.details())
        if self.columns:
            blocks.append(GridBlock(rows=self.columns))

        return blocks

    def trace(self) -> str:
        return self.cause.trace()

    def llm_view(self) -> str:
        parts: list[str] = [self.cause.llm_view()]
        if self.columns:
            parts.append(
                f"columns: {json.dumps(list(self.columns), ensure_ascii=False)}"
            )

        for statement in self.statements:
            parts.append(statement.llm_text(True))

        return "\n\n".join(parts)

    def chat_view(self) -> ChatView:
        parts: list[str] = [self.cause.chat_view().markdown]
        if self.columns:
            parts.append(TableText.render(self.columns))

        for statement in self.statements:
            parts.append(statement.markdown(True))

        return ChatView(markdown="\n\n".join(parts))


class ToolArtifact:
    """Поднимает artifact в модель: langgraph сериализует его в обычный dict.

    Чужое значение — строка старой истории, словарь без известного kind —
    даёт None: лента показывает его сырым текстом. Словарь известного kind
    с битыми полями — ValidationError: это дефект, а не чужое значение.
    История переживает версии результата: поля, которых у класса больше
    нет, отбрасываются, а не роняют загрузку треда.
    """

    _ADAPTER: ClassVar[TypeAdapter[ToolResultBase]] = TypeAdapter(ToolResult)

    @classmethod
    def revive(cls, artifact: Any) -> ToolResultBase | None:
        """Ошибки:
        ValidationError — kind известен, а поля модели не проходят.
        """
        if isinstance(artifact, ToolResultBase):
            return artifact

        if not isinstance(artifact, Mapping):
            return None

        model = ResultKinds.of(str(artifact.get("kind")))
        if model is None:
            return None

        return cls._ADAPTER.validate_python(cls._known_fields(model, artifact))

    @staticmethod
    def _known_fields(
        model: type[ToolResultBase], artifact: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Поля артефакта, известные текущей модели вида."""
        kept: dict[str, Any] = {}
        for name, value in artifact.items():
            if name not in model.model_fields:
                continue

            kept[name] = value

        return kept
