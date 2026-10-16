"""Результат инструмента в формате MCP.

Сервер инструментов отдаёт итог вызова стандартными полями результата MCP:
блоки для модели и человека (content), признак сбоя (isError), длительность
вызова в _meta и structuredContent — только собственные данные результата
по outputSchema инструмента. Перевод описан в одном месте — ResultWire.

Здесь же модели границы клиент—сервер: служебные поля запроса
(RequestFields), аргументы чтения журнала (JournalRead) и настройки
расширений сервера (JournalFeature, FilesFeature).

Ошибки:
ValidationError — служебные поля запроса либо настройки расширения не
    проходят свою модель.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import quote, urlunsplit

import mcp_types as mt
from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.channels import JournalChannel
from boba.toolkit.dag import NodeOutcome, ServerFeature
from boba.toolkit.result import BlockAudience, ContentOut

__all__ = [
    "FilesFeature",
    "JournalFeature",
    "JournalRead",
    "RequestFields",
    "ResultWire",
    "WireResult",
]


class RequestFields(BaseModel):
    """Служебные поля запроса tools/call в _meta, моделью.

    Сервер разбирает ею _meta запроса (SentMeta). progress_token —
    стандартный токен прогресса запроса; None — клиент о ходе вызова не
    просил. Остальные ключи _meta модель пропускает.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    progress_token: mt.ProgressToken | None = Field(
        default=None, validation_alias="progressToken"
    )


class JournalFeature(ServerFeature):
    """Настройки расширения сервера «журнал вызовов».

    Сервер объявляет их клиенту при подключении словарём settings(); клиент
    разбирает объявление этой же моделью один раз, в open(). read — имя
    инструмента, читающего журнал окнами. path — путь маршрута, по которому
    канал журнала скачивается целиком: `{path}/{run}/{node}/{channel}` (GET,
    потоком, с Range). resource — шаблон uri (RFC 6570) ресурса канала
    журнала с переменными run, node и channel: итог вызова несёт ссылки
    resource_link на каналы его журнала по этому шаблону.
    """

    ID: ClassVar[str] = "com.boba/journal"

    read: str = Field(min_length=1)
    path: str = Field(min_length=1)
    resource: str = Field(min_length=1)


class FilesFeature(ServerFeature):
    """Настройки расширения сервера «файлы workspace».

    Сервер объявляет их клиенту при подключении словарём settings(); клиент
    разбирает объявление этой же моделью один раз, в open(). path — путь
    маршрута файлов на сервере: файл области лежит на
    `{path}/{scope}/{dir}/{name}` (PUT — запись потоком, GET — чтение с
    Range, HEAD — размер и версия, DELETE — удаление). upload — имя
    инструмента, которым модель узнаёт адрес загрузки файла. workspace —
    каталог workspace глазами инструментов сервера: пути файлов в их
    результатах начинаются с него.
    """

    ID: ClassVar[str] = "com.boba/files"

    path: str = Field(min_length=1)
    upload: str = Field(min_length=1)
    workspace: str = Field(min_length=1)


class JournalRead(BaseModel):
    """Аргументы чтения журнала вызова: окно от offset либо перед before.

    Адрес журнала — запуск run и идентификатор вызова на сервере node: оба
    приходят ссылкой resource_link на канал журнала в итоге вызова.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: str = Field(
        min_length=1, description="Run id: the `run` of the call result or signal."
    )
    node: str = Field(min_length=1, description="Call id of the tool call (node).")
    channel: JournalChannel = Field(description="Journal channel of the call.")
    offset: int | None = Field(
        default=None,
        description=(
            "Read the window starting at this byte; a negative value reads the "
            "last window of the journal."
        ),
    )
    before: int | None = Field(
        default=None, ge=0, description="Read the window ending before this byte."
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
    сегментом — по нему клиент подписывает блок.
    """

    RESOURCE_SCHEME: ClassVar[str] = "boba-result"

    def __init__(self, call_id: str) -> None:
        self._call_id = call_id
        self._blocks: list[mt.ContentBlock] = []

    def text(self, markdown: str, audience: BlockAudience) -> None:
        annotations = None
        if audience is not BlockAudience.BOTH:
            annotations = self._for(audience)

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
        annotations = self._for(BlockAudience.USER)
        block = mt.EmbeddedResource(resource=contents, annotations=annotations)
        self._blocks.append(block)

    def link(self, uri: str, name: str, mime: str) -> None:
        annotations = self._for(BlockAudience.USER)
        block = mt.ResourceLink(
            uri=uri, name=name, mime_type=mime, annotations=annotations
        )
        self._blocks.append(block)

    def __len__(self) -> int:
        return len(self._blocks)

    def blocks(self) -> Sequence[mt.ContentBlock]:
        return list(self._blocks)

    def _for(self, audience: BlockAudience) -> mt.Annotations:
        return mt.Annotations.model_validate({"audience": [audience.value]})


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
