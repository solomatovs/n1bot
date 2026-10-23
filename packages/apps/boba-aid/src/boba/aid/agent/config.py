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
from typing import Annotated, Any, Literal

from psycopg.conninfo import make_conninfo
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import PasswordAuth, PostgresKerberos, TrustAuth
from boba.kerberos import KerberosPasswordAuth, KeytabAuth
from boba.mcp_client.client import HttpLocation, ProxyAuth

__all__ = [
    "AgentCommandConfig",
    "AgentConfig",
    "AgentHistoryConfig",
    "AgentHistoryKind",
    "AgentModelConfig",
    "AgentServiceConfig",
    "DsnKey",
    "LibpqConnection",
    "MemoryAgentHistory",
    "PostgresAgentHistory",
    "ReasoningReturn",
]


class AgentHistoryKind(StrEnum):
    """Виды хранилища истории агента, которые чат умеет описать в файле
    настроек: значения — ключ `history.kind` договора запуска."""

    POSTGRES = "postgres"
    MEMORY = "memory"


class ReasoningReturn(StrEnum):
    """Возврат рассуждений прошлых ответов модели: значения — ключ
    `reasoningReturn` профиля модели в файле настроек агента."""

    NEVER = "never"
    TURN = "turn"
    ALWAYS = "always"


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
    """Агент — служба boba-agent-service по адресу ACP (streamable HTTP):
    процессы агента, профили моделей и историю держит служба. Чат входит в
    неё proxy от имени пользователя вкладки и выбирает профиль агента
    настройкой сессии `model` по имени профиля чата."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: HttpLocation = Field(
        description="Адрес ACP службы: схема, хост, порт и путь ([agent].path службы)."
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


class DsnKey(StrEnum):
    """Поля PostgresConfig, которые уходят в строку подключения libpq агента."""

    HOST = "host"
    HOSTADDR = "hostaddr"
    PORT = "port"
    DBNAME = "dbname"
    SSLMODE = "sslmode"
    CONNECT_TIMEOUT = "connect_timeout"


class DsnAuthKey(StrEnum):
    """Ключи libpq роли и пароля в строке подключения."""

    USER = "user"
    PASSWORD = "password"  # noqa: S105 — имя ключа libpq


class PostgresAgentHistory(BaseModel):
    """История агента в Postgres по подключению процесса [postgres] (роль с
    паролем, доверием pg_hba либо билетом Kerberos) в своей схеме.

    Агент принимает строку libpq: её и окружение libkrb5 собирает
    LibpqConnection; что подключение процесса ею выразимо, проверяет AppConfig.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: Literal[AgentHistoryKind.POSTGRES]
    db_schema: str = Field(alias="schema", min_length=1)


class LibpqConnection:
    """Подключение процесса [postgres] глазами агента: строка libpq файла
    настроек и kerberos-вариант входа для окружения libkrb5.

    Агент принимает строку libpq: пароль идёт в ней, kerberos-роль входит
    билетом из кэша, путь к которому процесс получает переменными libkrb5
    (KRB5CCNAME, KRB5_CONFIG). Строят его сборка файла настроек
    (SettingsFileBuilder), окружение процесса агента (HistoryEnvironment) и
    проверка AppConfig на старте.
    """

    def __init__(self, postgres: PostgresConfig) -> None:
        self._postgres = postgres

    def auth(self) -> PasswordAuth | TrustAuth | KeytabAuth | KerberosPasswordAuth:
        """Авторизация, выразимая строкой libpq и окружением libkrb5.

        Ошибки:
        ValueError — у подключения сертификатная, делегированная или билетная
            авторизация.
        """
        auth = self._postgres.auth
        if isinstance(auth, PasswordAuth | TrustAuth):
            return auth

        if isinstance(auth, KeytabAuth | KerberosPasswordAuth):
            return auth

        msg = (
            f"[postgres] auth {auth.method!r} cannot be handed to the agent "
            "history as a libpq connection string; expected method 'password', "
            "'trust', 'kerberos_keytab' or 'kerberos_password'"
        )
        raise ValueError(msg)

    def kerberos_auth(self) -> KeytabAuth | KerberosPasswordAuth | None:
        """Kerberos-вариант авторизации; None — роль входит паролем или доверием."""
        auth = self.auth()
        if isinstance(auth, KeytabAuth | KerberosPasswordAuth):
            return auth

        return None

    def dsn(self) -> SecretStr:
        """Строка подключения libpq: с паролем у парольной роли, без пароля у
        kerberos-роли (её имя — короткое имя принципала); уходит в файл 0600."""
        parts: dict[str, Any] = {}
        for key in DsnKey:
            value = getattr(self._postgres, key.value)
            if value is None:
                continue

            parts[key.value] = value

        auth = self.auth()
        if isinstance(auth, PasswordAuth | TrustAuth):
            parts[DsnAuthKey.USER.value] = auth.user

        if isinstance(auth, PasswordAuth):
            parts[DsnAuthKey.PASSWORD.value] = auth.password.get_secret_value()

        if isinstance(auth, KeytabAuth | KerberosPasswordAuth):
            parts[DsnAuthKey.USER.value] = PostgresKerberos.role_of(auth.principal)

        return SecretStr(make_conninfo(**parts))


class MemoryAgentHistory(BaseModel):
    """История только в памяти процесса агента: без хранилища `session/resume`
    отдаёт пустую беседу."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[AgentHistoryKind.MEMORY]


AgentHistoryConfig = Annotated[
    PostgresAgentHistory | MemoryAgentHistory, Field(discriminator="kind")
]


class AgentModelConfig(BaseModel):
    """Секция [agent.model]: что агент знает о модели профилей сверх профиля
    чата — окно, потолок ответа и возврат рассуждений."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window: int = Field(gt=0, description="Окно контекста модели, токены.")
    max_output_tokens: int = Field(gt=0, description="Потолок ответа модели.")
    reasoning_return: ReasoningReturn = Field(
        description=(
            "Возвращать ли модели её рассуждения прошлых ответов: never, turn "
            "(в пределах хода — так требуют thinking-модели вроде deepseek при "
            "вызове инструмента), always."
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
    model: AgentModelConfig
