"""Секция [agent] конфига aid: как достучаться до агента ACP — своим
процессом по команде или службой агента по адресу с входом proxy, —
хранилище его истории и адрес чата aid глазами агента. Значения, которые уходят в файл
настроек агента (вид истории, возврат рассуждений), — своими enum'ами по
договору запуска, моделей агента чат не импортирует.

Ошибки: своих не выпускает; разбор — pydantic при загрузке AppConfig.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from boba.db.postgres.connection import (
    LibpqConnection,
    PostgresAuthError,
    PostgresConfig,
)
from boba.mcp_client.client import HttpRoute, ProxyAuth

__all__ = [
    "AgentCommandConfig",
    "AgentConfig",
    "AgentHistoryConfig",
    "AgentHistoryKind",
    "AgentServiceConfig",
    "MemoryAgentHistory",
    "PostgresAgentHistory",
]


class AgentHistoryKind(StrEnum):
    """Виды хранилища истории агента, которые чат умеет описать в файле
    настроек: значения — ключ `history.kind` договора запуска."""

    POSTGRES = "postgres"
    MEMORY = "memory"


class AgentCommandConfig(BaseModel):
    """Как запускать агента: программа строкой оболочки, аргументы до
    `--settings`, сроки и корень приватных каталогов. Всё, что агент знает о
    работе, ему несёт файл настроек; окружение ребёнка — минимальный набор SDK."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str = Field(
        min_length=1,
        description=(
            "Программа агента строкой оболочки (разбор shlex): бинарник `boba` "
            "или интерпретатор с модулем `python -m boba.cli.app`."
        ),
    )
    args: Sequence[str] = Field(
        description="Аргументы до `--settings <файл>`: подкомандa acp и её ключи."
    )
    private_dir: str = Field(
        min_length=1,
        description=(
            "Корень приватных каталогов процессов: на запуск создаётся свой "
            "каталог 0700 с файлом настроек 0600, удаляется с процессом."
        ),
    )
    start_timeout_sec: float = Field(
        gt=0, description="Сколько ждать рукопожатия initialize после запуска."
    )
    request_timeout_sec: float = Field(
        gt=0,
        description=(
            "Сколько ждать ответа агента на метод сессии (new, resume, fork, "
            "prompt, close, delete); ход ответа не ждёт — его конец приходит idle."
        ),
    )
    stop_timeout_sec: float = Field(
        gt=0, description="Сколько ждать выхода процесса после конца ввода."
    )

    @field_validator("command")
    @classmethod
    def _parsed(cls, value: str) -> str:
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            msg = (
                f"[agent.process] command {value!r} is not a shell command line: {exc}"
            )
            raise ValueError(msg) from exc

        if not parts:
            msg = f"[agent.process] command {value!r} names no program"
            raise ValueError(msg)

        return value

    def program(self) -> tuple[str, ...]:
        """Программа агента словами: исполняемый файл и его первые аргументы."""
        return tuple(shlex.split(self.command))


class AgentServiceConfig(BaseModel):
    """Агент — служба агента (`boba serve`) по адресу ACP (streamable HTTP):
    сессии агента, профили моделей и историю держит служба. Чат входит в
    неё proxy от имени пользователя вкладки и выбирает профиль агента
    настройкой сессии `model` по имени профиля чата."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: HttpRoute = Field(
        description=(
            "Адрес ACP службы: сервис [service.boba] (схема, хост, порт и "
            "префикс) и путь ACP под ним ([agent].path службы)."
        )
    )
    auth: ProxyAuth = Field(
        description=(
            "Вход proxy в службу: издатель (public_url службы), клиент OAuth "
            "([agent.clients] службы) и ключ утверждений ([auth.proxy] службы); "
            "логин и роли подставляются пользователем вкладки."
        )
    )
    start_timeout_sec: float = Field(
        gt=0, description="Сколько ждать входа и рукопожатия initialize."
    )
    request_timeout_sec: float = Field(
        gt=0,
        description=(
            "Сколько ждать ответа службы на метод сессии; ход ответа не ждёт — "
            "его конец приходит idle."
        ),
    )

    def peer(self) -> str:
        """Служба агента для человека: её адрес ACP."""
        return f"agent service at {self.endpoint.url()}"


class PostgresAgentHistory(BaseModel):
    """История агента в Postgres своим подключением ([postgres_history]: своя
    роль с паролем, доверием pg_hba либо своим принципалом Kerberos) в своей
    схеме. Подключение чата [postgres] истории не касается.

    Агент принимает строку libpq: её и окружение libkrb5 собирает
    LibpqConnection (boba-db-postgres); что вход ею выразим, проверяет модель.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: Literal[AgentHistoryKind.POSTGRES]
    db_schema: str = Field(alias="schema", min_length=1)
    postgres: PostgresConfig = Field(
        description=(
            "Подключение истории ([postgres_history]): строкой libpq оно уходит "
            "агенту в файл настроек."
        )
    )

    @field_validator("postgres")
    @classmethod
    def _fits_libpq(cls, value: PostgresConfig) -> PostgresConfig:
        try:
            LibpqConnection(value).auth()
        except PostgresAuthError as exc:
            msg = f"[agent.history].postgres: {exc}"
            raise ValueError(msg) from exc

        return value


class MemoryAgentHistory(BaseModel):
    """История только в памяти процесса агента: без хранилища `session/resume`
    отдаёт пустую беседу."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[AgentHistoryKind.MEMORY]


AgentHistoryConfig = Annotated[
    PostgresAgentHistory | MemoryAgentHistory, Field(discriminator="kind")
]


class AgentLimitsConfig(BaseModel):
    """Секция [agent.limits]: пределы хода агента, которые задаёт чат."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_result_window_share: float = Field(
        gt=0,
        le=1,
        description=(
            "Доля действующего окна модели, которую может занять один результат "
            "инструмента; длиннее — агент обрезает его с пометкой для модели."
        ),
    )


class AgentToolsConfig(BaseModel):
    """Секция [agent.tools]: какие инструменты модель агента получает полным
    определением с первого запроса."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    always_load: Sequence[str] = Field(
        description=(
            "Полные имена инструментов, как их видит модель "
            "(mcp__<сервер>__<инструмент>), которые уходят модели сразу; "
            "остальные отложены — модель видит их имена и загружает "
            "инструментом ToolSearch. Пустой список — всё через ToolSearch."
        )
    )


class AgentConfig(BaseModel):
    """Секция [agent]: всё, что чат aid знает об агенте и о себе для него."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    process: AgentCommandConfig | AgentServiceConfig = Field(
        description=(
            "Агент: свой процесс по команде (command, args, private_dir) либо "
            "служба агента по адресу (endpoint, auth)."
        )
    )
    history: AgentHistoryConfig
    tools: AgentToolsConfig
    limits: AgentLimitsConfig
    public_url: str = Field(
        min_length=1,
        description=(
            "Адрес чата aid: схема, хост, порт и префикс приложения. По нему "
            "агент берёт вложения сообщений пользователя."
        ),
    )
    seal_ttl_sec: int = Field(
        gt=0,
        description=(
            "Срок годности соединения, запечатанного ретранслятором для сервера "
            "инструментов, в секундах: сервер отвергает значение старше срока."
        ),
    )
    question_timeout_sec: int = Field(
        gt=0,
        description=(
            "Сколько ждать ответа пользователя на вопрос агента (форма "
            "elicitation/create), в секундах: по истечении вопрос снимается "
            "ответом cancel, ход идёт дальше."
        ),
    )
