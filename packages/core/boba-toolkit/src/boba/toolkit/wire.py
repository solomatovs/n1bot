"""Результат инструмента в формате MCP.

Сервер инструментов отдаёт итог вызова стандартными полями результата MCP:
блоки для модели и человека (content), признак сбоя (isError), длительность
вызова в _meta и structuredContent — только собственные данные результата
по outputSchema инструмента. Перевод описан в одном месте — ResultWire.

Здесь же модели границы клиент—сервер: адрес окна журнала (JournalRead),
уведомление об открытом журнале вызова (JournalNotice) и настройки
расширений сервера (JournalFeature, FilesFeature, ScopeFeature).

Ошибки:
ValidationError — настройки расширения не проходят свою модель.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import quote, urlunsplit

import mcp_types as mt
from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.calls import SchemaMark
from boba.toolkit.channels import JournalChannel
from boba.toolkit.dag import NodeOutcome, ServerFeature
from boba.toolkit.result import BlockAudience, ContentOut

__all__ = [
    "FilesFeature",
    "JournalFeature",
    "JournalNotice",
    "JournalRead",
    "ResultWire",
    "ScopeFeature",
    "WireResult",
]


class JournalFeature(ServerFeature):
    """Настройки расширения сервера «журнал вызовов».

    Сервер объявляет их в своём описании словарём settings(); клиент
    разбирает объявление этой же моделью из описания сервера и объявляет
    расширение в своих возможностях пустыми настройками. path — путь
    маршрута, по которому канал журнала скачивается целиком:
    `{path}/{run}/{node}/{channel}` (GET, потоком, с Range). resource —
    шаблон uri (RFC 6570) ресурса канала журнала с переменными run, node и
    channel и запросом offset, before: resources/read по нему отдаёт окно
    канала, координаты окна — в _meta содержимого под ключом ID. Итог
    вызова несёт ссылки resource_link на каналы его журнала по этому
    шаблону. Пока вызов идёт, сервер сообщает о каждом открытом им журнале
    уведомлением OPENED (параметры — JournalNotice) в потоке ответа самого
    tools/call, если клиент объявил расширение и назвал токен прогресса.
    Клиенту без расширения уведомления не идут: ему остаётся путь ядра —
    журналы идущих вызовов ресурсами resources/list со сменой списка и
    ссылки в итоге.
    """

    ID: ClassVar[str] = "com.boba/journal"
    ROUTE: ClassVar[str] = "{run}/{node}/{channel}"
    """Канал журнала под путём path и в uri ресурса: одна запись на сервер
    (маршрут и шаблон ресурса) и на клиент (JournalRead.route)."""
    OPENED: ClassVar[str] = "notifications/com.boba/journal/opened"
    """Метод уведомления об открытом журнале идущего вызова."""

    path: str = Field(min_length=1)
    resource: str = Field(min_length=1)


class JournalNotice(mt.NotificationParams):
    """Параметры уведомления JournalFeature.OPENED: вызов открыл журнал.

    progress_token — токен прогресса того запроса tools/call, в потоке
    ответа которого идёт уведомление: по нему клиент отдаёт его обработчику
    хода своего вызова, как notifications/progress. run и node — адрес
    журнала (запуск и идентификатор вызова на сервере), channels — его
    каналы для человека. key — ключ узла от автора связки, когда журнал
    принадлежит узлу вызова со вложенными вызовами; у вызова одного
    инструмента его нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    progress_token: mt.ProgressToken
    run: str = Field(min_length=1)
    node: str = Field(min_length=1)
    channels: tuple[JournalChannel, ...] = Field(min_length=1)
    key: str | None = None


class FilesFeature(ServerFeature):
    """Настройки расширения сервера «файлы workspace».

    Сервер объявляет их в своём описании словарём settings(); клиент
    разбирает объявление этой же моделью из описания сервера. path — путь
    маршрута файлов на сервере: файл области лежит на
    `{path}/{scope}/{dir}/{name}` (PUT — запись потоком, GET — чтение с
    Range, HEAD — размер и версия, DELETE — удаление). upload — имя
    инструмента, которым модель узнаёт адрес загрузки файла. workspace —
    каталог workspace глазами инструментов сервера: пути файлов в их
    результатах начинаются с него.
    """

    ID: ClassVar[str] = "com.boba/files"
    ROUTE: ClassVar[str] = "{scope}/{dir}/{name}"
    """Файл области под путём path: одна запись на маршрут сервера и адреса
    клиента."""
    AREA: ClassVar[str] = "{scope}"
    """Область целиком под путём path: DELETE удаляет её файлы и журналы."""

    path: str = Field(min_length=1)
    upload: str = Field(min_length=1)
    workspace: str = Field(min_length=1)


class ScopeFeature(ServerFeature):
    """Настройки расширения сервера «область вызова».

    Сервер объявляет их в своём описании словарём settings(); клиент
    разбирает объявление этой же моделью из описания сервера и объявляет
    расширение в своих возможностях пустыми настройками. mark — ключ JSON
    Schema, которым сервер помечает параметр области вызова в схемах своих
    инструментов. Область — непрозрачный для сервера идентификатор, который
    клиент даёт вызовам, работающим над одними файлами: клиент с расширением
    прячет помеченный параметр от модели и ставит в него свою область сам,
    клиент без расширения показывает параметр модели как есть. Пустое
    значение — у сервера область по умолчанию, личная у каждого вызывающего.
    """

    ID: ClassVar[str] = "com.boba/scope"

    mark: str = Field(min_length=1)

    def params(self, schema: Mapping[str, Any]) -> Mapping[str, object]:
        """Параметры схемы инструмента schema с пометкой области: имя →
        значение пометки."""
        return SchemaMark.marked(schema, self.mark)


class JournalRead(BaseModel):
    """Адрес окна журнала вызова: окно от offset либо перед before; без
    обоих — канал целиком от начала, как его называют ссылки.

    Адрес журнала — запуск run и идентификатор вызова на сервере node: оба
    приходят ссылкой resource_link на канал журнала в итоге вызова,
    уведомлением об открытом журнале (JournalNotice) либо ресурсом открытого
    журнала в resources/list.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: str = Field(
        min_length=1, description="id запуска: поле run итога вызова или сигнала."
    )
    node: str = Field(min_length=1, description="id вызова инструмента (узла).")
    channel: JournalChannel = Field(description="Канал журнала вызова.")
    offset: int | None = Field(
        default=None,
        description=(
            "Начало окна:\n"
            "   - байт — окно, которое начинается с этого байта\n"
            "   - отрицательное число — последнее окно журнала\n"
        ),
    )
    before: int | None = Field(
        default=None, ge=0, description="Окно, которое кончается перед этим байтом."
    )

    def route(self) -> str:
        """Канал журнала под путём маршрута журналов (JournalFeature.path)."""
        return JournalFeature.ROUTE.format(
            run=self.run, node=self.node, channel=self.channel.value
        )


@dataclass(frozen=True)
class WireResult:
    """Итог вызова, как он едет по MCP.

    content — блоки для модели и человека; structured — structuredContent
    по outputSchema инструмента, None — у результата его нет; is_error —
    вызов кончился сбоем; meta — _meta ответа.
    """

    content: Sequence[mt.ContentBlock]
    structured: Mapping[str, Any] | None
    is_error: bool
    meta: Mapping[str, Any]


class McpContent(ContentOut):
    """Реализация ContentOut блоками MCP: content одного ответа на вызов.

    Создаёт его ResultWire.packed на каждый ответ; результат семейства пишет
    в него свои блоки (ToolResultBase.sent). Блок для одной стороны получает
    annotations.audience, общий — без неё. Встроенный ресурс получает uri
    схемы RESOURCE_SCHEME: вызов, номер блока и имя ресурса последним
    сегментом — по нему клиент подписывает блок. Ссылка на файл, который
    клиенту стоит показать сразу, получает стандартное annotations.priority
    SHOWN_PRIORITY.
    """

    RESOURCE_SCHEME: ClassVar[str] = "boba-result"

    SHOWN_PRIORITY: ClassVar[float] = 1.0
    """annotations.priority ссылки, которую клиенту стоит показать сразу."""

    def __init__(self, call_id: str) -> None:
        self._call_id = call_id
        self._blocks: list[mt.ContentBlock] = []

    def text(self, markdown: str, audience: BlockAudience) -> None:
        annotations = None
        if audience is not BlockAudience.BOTH:
            annotations = self._for(audience, None)

        block = mt.TextContent(text=markdown, annotations=annotations)
        self._blocks.append(block)

    def resource(self, mime: str, text: str, name: str) -> None:
        segments = (
            "",
            quote(self._call_id, safe=""),
            str(len(self._blocks)),
            quote(name, safe=""),
        )
        path = "/".join(segments)
        uri = urlunsplit((self.RESOURCE_SCHEME, "", path, "", ""))
        contents = mt.TextResourceContents(uri=uri, mime_type=mime, text=text)
        annotations = self._for(BlockAudience.USER, None)
        block = mt.EmbeddedResource(resource=contents, annotations=annotations)
        self._blocks.append(block)

    def link(self, uri: str, name: str, mime: str, shown: bool) -> None:
        priority = None
        if shown:
            priority = self.SHOWN_PRIORITY

        annotations = self._for(BlockAudience.USER, priority)
        block = mt.ResourceLink(
            uri=uri, name=name, mime_type=mime, annotations=annotations
        )
        self._blocks.append(block)

    def __len__(self) -> int:
        return len(self._blocks)

    def blocks(self) -> Sequence[mt.ContentBlock]:
        return list(self._blocks)

    def _for(self, audience: BlockAudience, priority: float | None) -> mt.Annotations:
        """Кому блок — модели или человеку (общий блок annotations не
        получает) — и насколько он важен; priority None — без пометки."""
        role: mt.Role = "user"
        if audience is BlockAudience.MODEL:
            role = "assistant"

        return mt.Annotations(audience=[role], priority=priority)


class ResultWire:
    """Перевод итога вызова в части MCP.

    Создаётся компонентом ответов сервера (McpReplies). Блоки результата
    (ToolResultBase.sent) едут в content, сбой — в isError, длительность
    вызова — в _meta, собственные данные результата по outputSchema
    (итог ToolResultBase.sent) — в structuredContent.
    """

    ELAPSED: ClassVar[str] = "com.boba/elapsed"
    """Ключ _meta ответа: длительность вызова в миллисекундах; без ключа —
    не измерена. Длительность узлов связки — в их записях (NestedCallReply)."""

    def packed(self, outcome: NodeOutcome) -> WireResult:
        """Итог вызова частями MCP."""
        disclosed = outcome.artifact.disclosed()
        content = McpContent(outcome.key)
        structured = disclosed.sent(content)

        meta: dict[str, Any] = {}
        if disclosed.elapsed_ms > 0:
            meta[self.ELAPSED] = disclosed.elapsed_ms

        return WireResult(
            content=content.blocks(),
            structured=structured,
            is_error=not outcome.ok(),
            meta=meta,
        )
