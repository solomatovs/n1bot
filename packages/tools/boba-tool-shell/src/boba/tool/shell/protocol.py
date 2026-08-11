"""Контракт узла bash: аргументы модели, запрос обвязки и описание стадии.

Обвязка bash — обычный payload-инструмент: команда приезжает в tool_args,
её stdout уходит в tool_payload, вход берётся из tool_stdin. Отказов операции
у обвязки два, оба объявлены в BashFailure.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.toolkit.channels import StreamFormat
from boba.toolkit.workflow import (
    EmptyTrailer,
    RequestArgs,
    StageContract,
    StageNode,
)

__all__ = [
    "BashArgs",
    "BashFailure",
    "BashRequest",
    "BashStage",
]

class BashFailure(StrEnum):
    """Ожидаемые отказы обвязки: kind конверта tool_result."""

    NOT_STARTED = "command_not_started"
    NO_OUTPUT_PIPE = "command_output_unavailable"


class BashArgs(BaseModel):
    """Пользовательская часть узла: то, что задаёт модель."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    MAX_COMMAND: ClassVar[int] = 16_384

    command: str = Field(min_length=1, max_length=MAX_COMMAND)


class BashRequest(BashArgs):
    """Запрос обвязки: команда плюс формат входного потока от источника.

    Формат объявлен закрытым enum'ом: при ребре его ставит раннер, у узла без
    входа остаётся значение по умолчанию — байты команде отдаются как есть.
    """

    stdin_format: StreamFormat = StreamFormat.TEXT


class BashStage:
    """Описание узла bash для реестра стадий: контракт, entry и обогатитель.

    bash — универсальный узел-адаптер: читает текстовые форматы, отдаёт stdout
    команды как text/plain. Профиль песочницы к узлу добавляет приложение.
    """

    NAME: ClassVar[str] = "bash"

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.shell.payload")

    CONTRACT: ClassVar[StageContract] = StageContract(
        accepts=frozenset(
            {StreamFormat.CSV, StreamFormat.NDJSON, StreamFormat.TEXT}
        ),
        out=StreamFormat.TEXT,
        result=EmptyTrailer,
    )

    @staticmethod
    def enrich(args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        """Args узла -> запрос обвязки; stdin_format добавляет раннер по ребру."""
        call = BashArgs.model_validate(args)

        return RequestArgs.of(call)

    @classmethod
    def stages(cls) -> dict[str, StageNode]:
        """Узел пакета для реестра стадий; профиль подставляет приложение."""
        node = StageNode(
            contract=cls.CONTRACT,
            entry=cls.ENTRY,
            request=BashRequest,
            enrich=cls.enrich,
        )

        return {cls.NAME: node}
