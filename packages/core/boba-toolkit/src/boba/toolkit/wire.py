"""Результат инструмента в формате MCP и обратно.

Сервер инструментов отдаёт итог вызова стандартными полями результата MCP:
блоки для модели (content), признак сбоя (isError) и structuredContent со
сведениями о вызове и собственными данными результата по outputSchema
инструмента, а у результата без неё — результатом семейства ToolResultBase.
Клиент собирает из них тот же итог NodeOutcome: класс результата
выбирается по kind через реестр видов, статус и адрес журнала — из
сведений о вызове. Обе стороны
пользуются одним ResultWire, поэтому формат описан в одном месте.

Здесь же модели границы клиент—сервер: служебные поля запроса
(RequestFields) и настройки расширений сервера (JournalFeature, FilesFeature).

Ошибки:
ValidationError — kind результата клиенту известен, а поля не проходят его
    модель; сведения о вызове, служебные поля запроса либо настройки
    расширения не проходят свою модель.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import quote, urlunsplit

import mcp_types as mt
from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.channels import JournalChannel
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
    NestedCallsOutput,
    NodeOutcome,
    NodeOutcomes,
    ServerFeature,
)
from boba.toolkit.result import BlockAudience, ContentOut, ToolArtifact

__all__ = [
    "CallStatus",
    "CallWire",
    "FilesFeature",
    "JournalFeature",
    "JournalRead",
    "NestedCallsReply",
    "RequestFields",
    "ResultWire",
    "WirePart",
    "WireResult",
]


class CallStatus(StrEnum):
    """Статус итога вызова: тот же, что у сообщения инструмента в истории."""

    SUCCESS = "success"
    ERROR = "error"


class RequestFields(BaseModel):
    """Служебные поля запроса tools/call в _meta, моделью.

    Сервер разбирает ею _meta запроса (SentMeta). scope — область вызова:
    её шлёт клиент, читающий расширение файлов (FilesFeature.scope называет
    ключ); пустое поле — клиент его не прислал. progress_token — стандартный
    токен прогресса запроса; None — клиент о ходе вызова не просил.
    Остальные ключи _meta модель пропускает.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    SCOPE: ClassVar[str] = "com.boba/scope"
    """Ключ области вызова в _meta запроса."""

    scope: str = Field(default="", validation_alias=SCOPE)
    progress_token: mt.ProgressToken | None = Field(
        default=None, validation_alias="progressToken"
    )


class JournalFeature(ServerFeature):
    """Настройки расширения сервера «журнал вызовов».

    Сервер объявляет их клиенту при подключении словарём settings(); клиент
    разбирает объявление этой же моделью один раз, в open(). read — имя
    инструмента, читающего журнал окнами. path — путь маршрута, по которому
    канал журнала скачивается целиком: `{path}/{run}/{node}/{channel}` (GET,
    потоком, с Range).
    """

    ID: ClassVar[str] = "com.boba/journal"

    read: str = Field(min_length=1)
    path: str = Field(min_length=1)


class FilesFeature(ServerFeature):
    """Настройки расширения сервера «файлы workspace».

    Сервер объявляет их клиенту при подключении словарём settings(); клиент
    разбирает объявление этой же моделью один раз, в open(). path — путь
    маршрута файлов на сервере: файл области лежит на
    `{path}/{scope}/{dir}/{name}` (PUT — запись потоком, GET — чтение с
    Range, HEAD — размер и версия, DELETE — удаление). upload — имя
    инструмента, которым модель узнаёт адрес загрузки файла. workspace —
    каталог workspace глазами инструментов сервера: пути файлов в их
    результатах начинаются с него. scope — ключ _meta запроса tools/call,
    которым клиент называет область вызова: вызовы одной области видят
    файлы друг друга; клиент без области работает в области пользователя.
    """

    ID: ClassVar[str] = "com.boba/files"

    path: str = Field(min_length=1)
    upload: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    scope: str = Field(min_length=1)


class JournalRead(BaseModel):
    """Аргументы чтения журнала вызова: окно от offset либо перед before.

    Адрес журнала — запуск run и идентификатор вызова на сервере node: оба
    приходят в сведениях о вызове итога (CallWire).
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


class WirePart(StrEnum):
    """Части structuredContent итога вызова: сведения о вызове и результат."""

    CALL = "call"
    RESULT = "result"


class CallWire(BaseModel):
    """Сведения о вызове в итоге, который сервер отдаёт клиенту.

    id — идентификатор вызова на сервере: под ним сервер ведёт журнал
    вызова. run — запуск, в котором вызов исполнен; пусто — вызов до запуска
    не дошёл. status — error, если вызов не дошёл до тела инструмента
    (аргументы, права, отказ сервера); тело, вернувшее отказ результатом,
    — success.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    status: CallStatus
    run: str = ""


class NestedCallsReply(NestedCallsOutput):
    """structuredContent итога инструмента со вложенными вызовами: сведения о
    вызове (CallWire) и узлы. Из неё outputSchema связки; клиент разбирает
    ею ответ связки."""

    call: CallWire


@dataclass(frozen=True)
class WireResult:
    """Итог вызова, как он едет по MCP.

    content — блоки для модели и человека; structured — structuredContent
    итога: части WirePart у ответа сервера инструментов, None — сервер его
    не прислал; is_error — вызов кончился сбоем; meta — _meta ответа.
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
            annotations = mt.Annotations.model_validate({"audience": [audience.value]})

        self._blocks.append(mt.TextContent(text=markdown, annotations=annotations))

    def resource(self, mime: str, text: str, name: str) -> None:
        segments = (
            "",
            quote(self._call_id, safe=""),
            str(len(self._blocks)),
            quote(name, safe=""),
        )
        address = (self.RESOURCE_SCHEME, "", "/".join(segments), "", "")
        contents = mt.TextResourceContents(
            uri=urlunsplit(address), mime_type=mime, text=text
        )
        self._blocks.append(
            mt.EmbeddedResource(resource=contents, annotations=self._for_user())
        )

    def link(self, uri: str, name: str, mime: str) -> None:
        self._blocks.append(
            mt.ResourceLink(
                uri=uri, name=name, mime_type=mime, annotations=self._for_user()
            )
        )

    def __len__(self) -> int:
        return len(self._blocks)

    def blocks(self) -> Sequence[mt.ContentBlock]:
        return list(self._blocks)

    def _for_user(self) -> mt.Annotations:
        return mt.Annotations.model_validate({"audience": [BlockAudience.USER.value]})


class ResultWire:
    """Перевод итога вызова в части MCP (сервер) и обратно (клиент).

    Создаётся компонентом ответов сервера (McpReplies) и разбором ответов в
    клиенте (McpToolServer). Итог едет стандартными полями результата MCP:
    блоки результата — в content, признак сбоя — в isError, остальное — в
    structuredContent: сведения о вызове (CallWire) и собственные данные
    результата (ToolResultBase.structured), а у результата без них —
    результат семейства частью WirePart.RESULT. По ним клиент
    восстанавливает итог таким, каким его отдал порт сервера, и узнаёт
    адрес журнала вызова.
    """

    ELAPSED: ClassVar[str] = "com.boba/elapsed"
    """Ключ _meta ответа: длительность вызова в миллисекундах; без ключа —
    не измерена. Длительность узлов связки — в их записях (NestedCallReply)."""

    def __init__(self) -> None:
        self._outcomes = NodeOutcomes()

    def packed(self, outcome: NodeOutcome) -> WireResult:
        """Итог вызова частями MCP; запуск дописывает stamped()."""
        status = CallStatus.SUCCESS
        if outcome.errored:
            status = CallStatus.ERROR

        failed = not outcome.ok()

        call = CallWire(id=outcome.key, status=status)
        disclosed = outcome.artifact.disclosed()
        structured: dict[str, Any] = {WirePart.CALL.value: call.model_dump(mode="json")}

        own = disclosed.structured(McpContent(outcome.key))
        if own is None:
            structured[WirePart.RESULT.value] = disclosed.model_dump(mode="json")
        else:
            structured.update(own)

        content = McpContent(outcome.key)
        disclosed.sent(content)

        meta: dict[str, Any] = {}
        if disclosed.elapsed_ms > 0:
            meta[self.ELAPSED] = disclosed.elapsed_ms

        return WireResult(
            content=content.blocks(), structured=structured, is_error=failed, meta=meta
        )

    def stamped(self, structured: Mapping[str, Any], run: str) -> dict[str, Any]:
        """Тот же structuredContent с запуском run в сведениях о вызове;
        без сведений о вызове (ответ операции сервиса) — как есть.

        Ошибки:
        ValidationError — сведения о вызове не проходят модель CallWire.
        """
        stamped = dict(structured)
        sent = structured.get(WirePart.CALL.value)
        if sent is None:
            return stamped

        call = CallWire.model_validate(sent).model_copy(update={"run": run})
        stamped[WirePart.CALL.value] = call.model_dump(mode="json")

        return stamped

    def revived(
        self, wire: WireResult, call: DagNode, server: str
    ) -> NodeOutcome | None:
        """Итог вызова call из частей MCP — обратный перевод packed. server —
        имя, под которым клиент знает сервер: с запуском и идентификатором
        вызова на сервере оно даёт адрес журнала. None — частей WirePart в
        structuredContent нет либо вид результата клиенту неизвестен.

        Ошибки:
        ValidationError — сведения о вызове либо поля результата известного
            вида не проходят свою модель.
        """
        structured = wire.structured
        if structured is None:
            return None

        sent = structured.get(WirePart.CALL.value)
        if sent is None:
            return None

        result = structured.get(WirePart.RESULT.value)
        if not isinstance(result, Mapping):
            return None

        artifact = ToolArtifact.revive(result)
        if artifact is None:
            return None

        served = CallWire.model_validate(sent)
        errored = served.status is CallStatus.ERROR
        outcome = self._outcomes.of(call, artifact, errored)
        if not served.run:
            return outcome

        address = JournalAddress(server=server, run=served.run, call=served.id)

        return outcome.model_copy(update={"journal": address})
