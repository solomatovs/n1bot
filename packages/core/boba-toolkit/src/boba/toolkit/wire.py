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

from boba.toolkit.result import FailureResult, ToolArtifact, ToolResultBase

__all__ = ["CallStatus", "ResultWire", "RevivedResult", "WireMeta", "WireResult"]


class CallStatus(StrEnum):
    """Статус итога вызова: тот же, что у сообщения инструмента в истории."""

    SUCCESS = "success"
    ERROR = "error"


class WireMeta(StrEnum):
    """Ключи служебных полей итога в _meta результата MCP."""

    NAMESPACE = "boba"
    STATUS = "status"
    CALL_ID = "call_id"


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
