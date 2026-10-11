"""Секция [agent] конфига chainlit: команда запуска агента ACP, хранилище его
истории, адрес chainlit глазами агента, сервер инструментов чата и хук.

Ошибки: своих не выпускает; разбор — pydantic при загрузке AppConfig.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import PasswordAuth, PostgresKerberos, TrustAuth
from boba.kerberos import KerberosPasswordAuth, KeytabAuth

__all__ = [
    "AgentCommandConfig",
    "AgentConfig",
    "AgentHistoryConfig",
    "AgentHistoryKind",
    "ChatMcpConfig",
    "DsnKey",
    "HookConfig",
    "MemoryAgentHistory",
    "ModelLimitsConfig",
    "PostgresAgentHistory",
]


class AgentCommandConfig(BaseModel):
    """Как запускать агента: исполняемый файл, аргументы до `--settings`,
    нечувствительные переменные окружения и корень приватных каталогов."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str = Field(
        min_length=1, description="Исполняемый файл агента: `boba` или python."
    )
    args: Sequence[str] = Field(
        description="Аргументы до `--settings <файл>`: подкомандa acp и её ключи."
    )
    env: Mapping[str, str] = Field(
        description=(
            "Переменные окружения ребёнка поверх минимального набора; секретов "
            "здесь не бывает — они едут файлом настроек."
        )
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
    stop_timeout_sec: float = Field(
        gt=0, description="Сколько ждать выхода процесса после конца ввода."
    )


class AgentHistoryKind(StrEnum):
    """Виды хранилища истории агента, которые умеет собрать chainlit."""

    POSTGRES = "postgres"
    MEMORY = "memory"


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
    """История агента в Postgres: подключение ролью с паролем, доверием pg_hba
    либо билетом Kerberos; схема по A0.

    Агент принимает строку libpq: пароль идёт в ней, kerberos-роль входит
    билетом из кэша, путь к которому процесс получает переменными libkrb5
    (KRB5CCNAME, KRB5_CONFIG) — их собирает HistoryEnvironment.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: Literal[AgentHistoryKind.POSTGRES]
    postgres: PostgresConfig
    db_schema: str = Field(alias="schema", min_length=1)

    @model_validator(mode="after")
    def _libpq_auth(self) -> Self:
        self.libpq_auth()

        return self

    def libpq_auth(
        self,
    ) -> PasswordAuth | TrustAuth | KeytabAuth | KerberosPasswordAuth:
        """Авторизация, выразимая строкой libpq и окружением libkrb5.

        Ошибки:
        ValueError — у подключения сертификатная, делегированная или билетная
            авторизация.
        """
        auth = self.postgres.auth
        if isinstance(auth, PasswordAuth | TrustAuth):
            return auth

        if isinstance(auth, KeytabAuth | KerberosPasswordAuth):
            return auth

        msg = (
            f"[agent.history] postgres auth {auth.method!r} cannot be handed to the "
            "agent as a libpq connection string; expected method 'password', "
            "'trust', 'kerberos_keytab' or 'kerberos_password'"
        )
        raise ValueError(msg)

    def kerberos_auth(self) -> KeytabAuth | KerberosPasswordAuth | None:
        """Kerberos-вариант авторизации; None — роль входит паролем или доверием."""
        auth = self.libpq_auth()
        if isinstance(auth, KeytabAuth | KerberosPasswordAuth):
            return auth

        return None

    def dsn(self) -> SecretStr:
        """Строка подключения libpq: с паролем у парольной роли, без пароля у
        kerberos-роли (её имя — короткое имя принципала); уходит в файл 0600."""
        parts: dict[str, Any] = {}
        for key in DsnKey:
            value = getattr(self.postgres, key.value)
            if value is None:
                continue

            parts[key.value] = value

        auth = self.libpq_auth()
        if isinstance(auth, PasswordAuth | TrustAuth):
            parts[DsnAuthKey.USER.value] = auth.user

        if isinstance(auth, PasswordAuth):
            parts[DsnAuthKey.PASSWORD.value] = auth.password.get_secret_value()

        if isinstance(auth, KeytabAuth | KerberosPasswordAuth):
            parts[DsnAuthKey.USER.value] = PostgresKerberos.role_of(auth.principal)

        return SecretStr(make_conninfo(**parts))


class MemoryAgentHistory(BaseModel):
    """История только в памяти процесса агента: без хранилища `session/load`
    отдаёт пустую беседу."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[AgentHistoryKind.MEMORY]


AgentHistoryConfig = Annotated[
    PostgresAgentHistory | MemoryAgentHistory, Field(discriminator="kind")
]


class ChatMcpConfig(BaseModel):
    """Сервер инструментов чата `/chat/mcp` в `mcpServers` сессии: имя сервера
    задаёт имена инструментов `mcp__<name>__<tool>` у агента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enable: bool = Field(
        description="Записывать ли сервер чата в mcpServers; сам сервер — W10."
    )
    name: str = Field(min_length=1, description="Имя сервера в mcpServers.")
    path: str = Field(
        min_length=1, description="Путь сервера под адресом chainlit: /chat/mcp."
    )


class HookConfig(BaseModel):
    """Хук PreToolUse агента: адрес обработчика chainlit и срок ответа."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enable: bool = Field(
        description="Класть ли секцию hooks в файл настроек; обработчик — W10."
    )
    path: str = Field(
        min_length=1,
        description="Путь обработчика под адресом chainlit: /chat/hooks/pre-tool-use.",
    )
    timeout_sec: float = Field(gt=0, description="Срок ответа обработчика.")


class ModelLimitsConfig(BaseModel):
    """Пределы профиля модели, которых нет в профиле чата."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window: int = Field(gt=0, description="Окно контекста модели, токены.")
    max_output_tokens: int = Field(gt=0, description="Потолок ответа модели.")


class AgentConfig(BaseModel):
    """Секция [agent]: всё, что chainlit знает об агенте и о себе для него."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    process: AgentCommandConfig
    history: AgentHistoryConfig
    public_url: str = Field(
        min_length=1,
        description=(
            "Адрес chainlit, по которому агент зовёт его сервер инструментов и "
            "хук: схема, хост, порт и префикс приложения."
        ),
    )
    chat_mcp: ChatMcpConfig
    hook: HookConfig
    limits: ModelLimitsConfig
