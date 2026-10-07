"""Результат инструмента в формате MCP и обратно.

Сервер инструментов отдаёт итог вызова стандартными полями результата MCP:
текст для модели (content), признак сбоя (isError) и structuredContent со
сведениями о вызове и результатом семейства ToolResultBase. Клиент собирает
из них тот же итог NodeOutcome: класс результата выбирается по kind через
реестр видов, статус и адрес журнала — из сведений о вызове. Обе стороны
пользуются одним ResultWire, поэтому формат описан в одном месте.

Здесь же модели границы клиент—сервер: служебные поля запроса
(RequestFields) и настройки расширений сервера (JournalFeature, FilesFeature).

Ошибки:
ValidationError — kind результата клиенту известен, а поля не проходят его
    модель; сведения о вызове, служебные поля запроса либо настройки
    расширения не проходят свою модель.
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
    "CallWire",
    "FilesFeature",
    "JournalFeature",
    "JournalRead",
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
    ключ). Пустое поле — клиент его не прислал; остальные ключи _meta (токен
    прогресса) модель пропускает.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    SCOPE: ClassVar[str] = "com.boba/scope"
    """Ключ области вызова в _meta запроса."""

    scope: str = Field(default="", validation_alias=SCOPE)


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
    результатах начинаются с него. scope — ключ _meta запроса tools/call,
    которым клиент называет область вызова: вызовы одной области видят
    файлы друг друга; клиент без области работает в области пользователя.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ID: ClassVar[str] = "com.boba/files"

    path: str = Field(min_length=1)
    upload: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    scope: str = Field(min_length=1)

    def settings(self) -> dict[str, object]:
        """Настройки расширения, как они едут клиенту."""
        return self.model_dump(mode="json")


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


@dataclass(frozen=True)
class WireResult:
    """Итог вызова, как он едет по MCP.

    content — текст для модели; structured — structuredContent итога: части
    WirePart у ответа сервера инструментов, None — сервер его не прислал;
    is_error — вызов кончился сбоем.
    """

    content: str
    structured: Mapping[str, Any] | None
    is_error: bool


class ResultWire:
    """Перевод итога вызова в части MCP (сервер) и обратно (клиент).

    Создаётся компонентом ответов сервера (McpReplies) и разбором ответов в
    клиенте (McpResults). Итог едет стандартными полями результата MCP:
    текст для модели — в content, признак сбоя — в isError, остальное — в
    structuredContent двумя частями (WirePart): сведения о вызове (CallWire)
    и результат семейства ToolResultBase. По ним клиент восстанавливает
    итог таким, каким его отдал порт сервера, и узнаёт адрес журнала вызова.
    """

    def __init__(self) -> None:
        self._outcomes = NodeOutcomes()

    def packed(self, outcome: NodeOutcome) -> WireResult:
        """Итог вызова частями MCP; запуск дописывает stamped()."""
        status = CallStatus.SUCCESS
        if outcome.errored:
            status = CallStatus.ERROR

        failed = outcome.errored
        if outcome.failed():
            failed = True

        call = CallWire(id=outcome.key, status=status)
        structured = {
            WirePart.CALL.value: call.model_dump(mode="json"),
            WirePart.RESULT.value: outcome.artifact.disclosed().model_dump(mode="json"),
        }

        return WireResult(
            content=outcome.content, structured=structured, is_error=failed
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
