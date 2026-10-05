"""Результат инструмента в формате MCP и обратно.

Сервер инструментов отдаёт итог вызова по MCP тремя частями: текст для
модели (content), результат семейства ToolResultBase словарём
(structuredContent) и служебные поля в _meta. Клиент собирает из них тот же
итог: класс результата выбирается по kind через реестр видов. Обе стороны
пользуются одним ResultWire, поэтому формат описан в одном месте.

Ошибки:
ValidationError — kind результата клиенту известен, а поля не проходят его
    модель.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.channels import JournalChannel
from boba.toolkit.result import FailureResult, ToolArtifact, ToolResultBase

__all__ = [
    "CallStatus",
    "FilesFeature",
    "JournalFeature",
    "JournalRead",
    "JournalSignal",
    "RequestMeta",
    "ResultWire",
    "RevivedResult",
    "WireMeta",
    "WireResult",
]


class CallStatus(StrEnum):
    """Статус итога вызова: тот же, что у сообщения инструмента в истории."""

    SUCCESS = "success"
    ERROR = "error"


class RequestMeta(StrEnum):
    """Ключи служебных полей запроса tools/call в _meta. Их ставит программа-
    клиент, не модель: идентификатор вызова модели и область (scope), в
    которой живут файлы вызова."""

    CALL_ID = "boba/tool_call_id"
    SCOPE = "boba/scope"


class WireMeta(StrEnum):
    """Ключи служебных полей итога в _meta результата MCP."""

    NAMESPACE = "boba"
    STATUS = "status"
    CALL_ID = "call_id"
    RUN = "run"


class JournalFeature(StrEnum):
    """Расширение сервера «журнал вызовов»: идентификатор и ключи настроек.

    READ — имя инструмента, читающего журнал окнами. PATH — путь маршрута,
    по которому канал журнала скачивается целиком:
    `{PATH}/{run}/{node}/{channel}` (GET, потоком, с Range)."""

    ID = "com.boba/journal"
    READ = "read"
    PATH = "path"


class FilesFeature(StrEnum):
    """Расширение сервера «файлы workspace»: идентификатор и ключи настроек.

    PATH — путь маршрута файлов на сервере: файл области лежит на
    `{PATH}/{scope}/{dir}/{name}` (PUT — запись потоком, GET — чтение с
    Range, HEAD — размер и версия, DELETE — удаление). UPLOAD — имя
    инструмента, которым модель узнаёт адрес загрузки файла. WORKSPACE —
    каталог workspace глазами инструментов сервера: пути файлов в их
    результатах начинаются с него."""

    ID = "com.boba/files"
    PATH = "path"
    UPLOAD = "upload"
    WORKSPACE = "workspace"


class JournalRead(BaseModel):
    """Аргументы чтения журнала вызова: окно от offset либо перед before.

    Адрес журнала — запуск run (приходит в сигнале роста и в _meta итога
    вызова) и узел node (идентификатор вызова).
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


class JournalSignal(BaseModel):
    """Сигнал роста журнала вызова: канал channel узла node запуска run
    дорос до size байт. Текста в сигнале нет — его читают окнами.

    Сервер шлёт сигнал JSON-строкой в поле message уведомления
    notifications/progress вызова; клиент разбирает его этой же моделью.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: str = Field(min_length=1)
    node: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    size: int = Field(ge=0)
    closed: bool
    note: str


@dataclass(frozen=True)
class WireResult:
    """Итог вызова, как он едет по MCP.

    content — текст для модели; structured — результат семейства словарём,
    None у ответа сервера без него; is_error — вызов кончился сбоем; meta —
    служебные поля итога под ключом WireMeta.NAMESPACE.
    """

    content: str
    structured: Mapping[str, Any] | None
    is_error: bool
    meta: Mapping[str, Any]


@dataclass(frozen=True)
class RevivedResult:
    """Итог вызова, собранный клиентом из частей MCP.

    artifact — результат семейства; None, когда сервер прислал вид, которого
    клиент не знает, либо не прислал structuredContent вовсе: тогда клиент
    показывает текст content.
    """

    content: str
    artifact: ToolResultBase | None
    status: CallStatus
    call_id: str


class ResultWire:
    """Перевод итога вызова в части MCP (сервер) и обратно (клиент).

    Сбой узнаётся двумя способами: статус вызова error — вызов не дошёл до
    тела (аргументы, права, отказ плана) — либо результат семейства сбоев:
    тело вернуло отказ упакованным результатом, и статус при этом success.
    Точный статус едет в meta, чтобы клиент восстановил его как был.
    """

    def packed(
        self, content: str, artifact: ToolResultBase, status: CallStatus, call_id: str
    ) -> WireResult:
        failed = status is CallStatus.ERROR
        if isinstance(artifact, FailureResult):
            failed = True

        meta = {
            WireMeta.NAMESPACE.value: {
                WireMeta.STATUS.value: status.value,
                WireMeta.CALL_ID.value: call_id,
            }
        }

        return WireResult(
            content=content,
            structured=artifact.model_dump(mode="json"),
            is_error=failed,
            meta=meta,
        )

    def stamped(self, meta: Mapping[str, Any], run: str) -> dict[str, Any]:
        """Служебные поля итога с идентификатором запуска: по нему клиент
        читает журнал вызова после ответа."""
        own = dict(meta.get(WireMeta.NAMESPACE.value, {}))
        own[WireMeta.RUN.value] = run

        stamped = dict(meta)
        stamped[WireMeta.NAMESPACE.value] = own

        return stamped

    def run_of(self, meta: Mapping[str, Any]) -> str:
        """Идентификатор запуска из служебных полей итога; пусто — его нет."""
        own = meta.get(WireMeta.NAMESPACE.value)
        if not isinstance(own, Mapping):
            return ""

        run = own.get(WireMeta.RUN.value)
        if isinstance(run, str):
            return run

        return ""

    def revived(self, wire: WireResult) -> RevivedResult:
        """Ошибки:
        ValidationError — kind известен, а поля модели не проходят.
        """
        own = wire.meta.get(WireMeta.NAMESPACE.value)

        return RevivedResult(
            content=wire.content,
            artifact=ToolArtifact.revive(wire.structured),
            status=self._status(wire, own),
            call_id=self._call_id(own),
        )

    @staticmethod
    def _status(wire: WireResult, own: object) -> CallStatus:
        """Статус из meta; сервер без наших полей — по признаку сбоя MCP."""
        if isinstance(own, Mapping):
            declared = own.get(WireMeta.STATUS.value)
            if declared == CallStatus.ERROR.value:
                return CallStatus.ERROR

            if declared == CallStatus.SUCCESS.value:
                return CallStatus.SUCCESS

        if wire.is_error:
            return CallStatus.ERROR

        return CallStatus.SUCCESS

    @staticmethod
    def _call_id(own: object) -> str:
        if not isinstance(own, Mapping):
            return ""

        call_id = own.get(WireMeta.CALL_ID.value)
        if isinstance(call_id, str):
            return call_id

        return ""
