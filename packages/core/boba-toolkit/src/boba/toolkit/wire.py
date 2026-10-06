"""Результат инструмента в формате MCP и обратно.

Сервер инструментов отдаёт итог вызова по MCP тремя частями: текст для
модели (content), результат семейства ToolResultBase словарём
(structuredContent) и служебные поля в _meta. Клиент собирает из них тот же
итог NodeOutcome: класс результата выбирается по kind через реестр видов,
статус и запуск — из _meta. Обе стороны пользуются одним ResultWire,
поэтому формат описан в одном месте.

Здесь же модели границы клиент—сервер: служебные поля запроса
(RequestFields) и настройки расширений сервера (JournalFeature, FilesFeature).

Ошибки:
ValidationError — kind результата клиенту известен, а поля не проходят его
    модель; служебные поля запроса либо настройки расширения не проходят
    свою модель.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.channels import JournalChannel
from boba.toolkit.dag import DagNode, JournalAddress, NodeOutcome, NodeOutcomes
from boba.toolkit.result import ToolArtifact

__all__ = [
    "CallStatus",
    "FilesFeature",
    "JournalFeature",
    "JournalRead",
    "JournalSignal",
    "RequestFields",
    "RequestMeta",
    "ResultWire",
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


class RequestFields(BaseModel):
    """Служебные поля запроса tools/call в _meta, моделью.

    Клиент (McpToolServer) собирает их и шлёт серверу словарём meta();
    сервер разбирает _meta запроса этой же моделью (SentMeta). Пустое поле —
    клиент его не прислал; остальные ключи _meta (токен прогресса) модель
    пропускает.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    call_id: str = Field(
        default="",
        validation_alias=RequestMeta.CALL_ID.value,
        serialization_alias=RequestMeta.CALL_ID.value,
    )
    scope: str = Field(
        default="",
        validation_alias=RequestMeta.SCOPE.value,
        serialization_alias=RequestMeta.SCOPE.value,
    )

    def meta(self) -> dict[str, str]:
        """Поля, как они едут в _meta запроса: пустые не шлются."""
        return self.model_dump(by_alias=True, exclude_defaults=True)


class JournalFeature(BaseModel):
    """Настройки расширения сервера «журнал вызовов».

    Сервер объявляет их клиенту при подключении словарём settings(); клиент
    разбирает объявление этой же моделью один раз, в open(). read — имя
    инструмента, читающего журнал окнами. path — путь маршрута, по которому
    канал журнала скачивается целиком: `{path}/{run}/{node}/{channel}` (GET,
    потоком, с Range).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ID: ClassVar[str] = "com.boba/journal"

    read: str = Field(min_length=1)
    path: str = Field(min_length=1)

    def settings(self) -> dict[str, object]:
        """Настройки расширения, как они едут клиенту."""
        return self.model_dump(mode="json")


class FilesFeature(BaseModel):
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

    model_config = ConfigDict(frozen=True, extra="forbid")

    ID: ClassVar[str] = "com.boba/files"

    path: str = Field(min_length=1)
    upload: str = Field(min_length=1)
    workspace: str = Field(min_length=1)

    def settings(self) -> dict[str, object]:
        """Настройки расширения, как они едут клиенту."""
        return self.model_dump(mode="json")


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


class ResultWire:
    """Перевод итога вызова в части MCP (сервер) и обратно (клиент).

    Создаётся компонентом ответов сервера (McpReplies) и разбором ответов в
    клиенте (McpResults). Сбой узнаётся двумя способами: статус вызова error
    — вызов не дошёл до тела (аргументы, права, отказ плана) — либо
    результат семейства сбоев: тело вернуло отказ упакованным результатом, и
    статус при этом success. Точный статус едет в meta, и клиент
    восстанавливает итог таким, каким его отдал порт сервера.
    """

    def __init__(self) -> None:
        self._outcomes = NodeOutcomes()

    def packed(self, outcome: NodeOutcome) -> WireResult:
        """Итог вызова частями MCP: идентификатор вызова и статус — в meta."""
        status = CallStatus.SUCCESS
        if outcome.errored:
            status = CallStatus.ERROR

        failed = outcome.errored
        if outcome.failed():
            failed = True

        meta = {
            WireMeta.NAMESPACE.value: {
                WireMeta.STATUS.value: status.value,
                WireMeta.CALL_ID.value: outcome.key,
            }
        }

        return WireResult(
            content=outcome.content,
            structured=outcome.artifact.model_dump(mode="json"),
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

    def revived(
        self, wire: WireResult, call: DagNode, server: str
    ) -> NodeOutcome | None:
        """Итог вызова call из частей MCP — обратный перевод packed. server —
        имя, под которым клиент знает сервер: с запуском из meta оно даёт
        адрес журнала вызова. None — результата семейства в частях нет:
        сервер прислал вид, которого клиент не знает, либо не прислал
        structuredContent вовсе.

        Ошибки:
        ValidationError — kind известен, а поля модели не проходят.
        """
        artifact = ToolArtifact.revive(wire.structured)
        if artifact is None:
            return None

        own = wire.meta.get(WireMeta.NAMESPACE.value)
        errored = self._status(wire, own) is CallStatus.ERROR
        outcome = self._outcomes.of(call, artifact, errored)

        run = self._run(own)
        if not run:
            return outcome

        address = JournalAddress(server=server, run=run)

        return outcome.model_copy(update={"journal": address})

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
    def _run(own: object) -> str:
        """Идентификатор запуска из служебных полей итога; пусто — его нет."""
        if not isinstance(own, Mapping):
            return ""

        run = own.get(WireMeta.RUN.value)
        if isinstance(run, str):
            return run

        return ""
