"""Модели секций AID и файла настроек, который чат передаёт агенту."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit

from acp.http.server import SSE_KEEPALIVE_INTERVAL_SECONDS
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from boba.aid.auth.cookies import CookieSpec
from boba.aid.auth.policy import SessionRenewal
from boba.auth.policy import SessionPolicy
from boba.chat.profiles import (
    ReasoningReturn,
)
from boba.core.secrets import SecretReveal
from boba.crypto.config import ValueCipherConfig
from boba.db.postgres.connection import (
    LibpqConnection,
    PostgresAuthError,
    PostgresConfig,
)
from boba.http_server import ServerConfig
from boba.mcp_client.client import HttpRoute, ProxyAuth
from boba.messaging.config import MessagingConfig
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import HttpConnection, WebAuth


class DataConfig(BaseModel):
    """Каталог данных экземпляра; имена подкаталогов передаёт потребитель."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "data"

    data_dir: str = Field(
        min_length=1,
        description="Абсолютный путь к данным экземпляра приложения.",
    )

    @model_validator(mode="before")
    @classmethod
    def _directory(cls, value: object) -> object:
        if isinstance(value, str):
            return {"data_dir": value}

        return value

    @field_validator("data_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            msg = f"data: expected an absolute path, got {value!r}"
            raise ValueError(msg)

        return os.path.normpath(value)

    def data(self, part: str) -> Path:
        return Path(self.data_dir) / part


class TlsConfig(BaseModel):
    """Доверенные CA исходящих подключений процесса и сетевых песочниц."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    SECTION: ClassVar[str] = "tls"
    CA_ENV: ClassVar[str] = "SSL_CERT_FILE"
    ca_file: str = Field(default="/etc/ssl/certs/ca-certificates.crt", min_length=1)

    @field_validator("ca_file")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            raise ValueError("[tls].ca_file must be an absolute path")
        return os.path.normpath(value)


class AgentHistoryKind(StrEnum):
    """Виды хранилища истории агента, которые чат умеет описать в файле
    настроек: значения — ключ `history.kind` договора запуска."""

    POSTGRES = "postgres"
    MEMORY = "memory"


class AgentCommandConfig(BaseModel):
    """Как запускать агента: программа строкой оболочки, аргументы до
    `--settings` и сроки. Приватные каталоги процессов лежат в agents/
    каталога данных экземпляра. Всё, что агент знает о работе, ему несёт файл
    настроек; окружение ребёнка — минимальный набор SDK."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    command: str = Field(
        min_length=1,
        description=(
            "Программа агента строкой оболочки (разбор shlex): бинарник `boba` "
            "или интерпретатор с модулем `python -m boba.cli.app`."
        ),
    )
    args: Sequence[str] = Field(
        default=("--stdio",),
        description="Аргументы запуска ACP до `--settings <файл>`.",
    )
    start_timeout_sec: float = Field(
        default=30,
        gt=0,
        description="Сколько ждать рукопожатия initialize после запуска.",
    )
    request_timeout_sec: float = Field(
        default=120,
        gt=0,
        description=(
            "Сколько ждать ответа агента на метод сессии (new, resume, fork, "
            "close, delete). Вопрос (prompt) ждёт без срока: агент отвечает, "
            "когда вставил его своим ходом, а вопрос во время хода — после "
            "идущего хода; ушедший процесс обрывает ожидание."
        ),
    )
    stop_timeout_sec: float = Field(
        default=10,
        gt=0,
        description="Сколько ждать выхода процесса после конца ввода.",
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

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    endpoint: HttpRoute = Field(
        description=(
            "Полный URL ACP службы, включая префикс и путь ACP "
            "([agent].path в конфиге службы)."
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
        default=60,
        gt=0,
        description="Сколько ждать входа и рукопожатия initialize.",
    )
    request_timeout_sec: float = Field(
        default=120,
        gt=0,
        description=(
            "Сколько ждать ответа службы на метод сессии. Вопрос (prompt) ждёт "
            "без срока: служба отвечает, когда вставила его своим ходом, а "
            "вопрос во время хода — после идущего хода; закрытый или "
            "замолчавший поток событий обрывает ожидание."
        ),
    )
    silence_timeout_sec: float = Field(
        default=60,
        gt=SSE_KEEPALIVE_INTERVAL_SECONDS,
        description=(
            "Сколько может молчать поток событий службы, прежде чем чат сочтёт "
            "службу ушедшей: служба шлёт в него keepalive каждые "
            f"{SSE_KEEPALIVE_INTERVAL_SECONDS:g} с, срок должен быть больше."
        ),
    )

    def peer(self) -> str:
        """Служба агента для человека: её адрес ACP."""
        return f"agent service at {self.endpoint.url()}"


class PostgresAgentHistory(BaseModel):
    """История агента в Postgres своим подключением ([agent.history.postgres]: своя
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
            "Подключение истории ([agent.history.postgres]): строкой libpq оно уходит "
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

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    kind: Literal[AgentHistoryKind.MEMORY]


AgentHistoryConfig = Annotated[
    PostgresAgentHistory | MemoryAgentHistory, Field(discriminator="kind")
]


class AgentLimitsConfig(BaseModel):
    """Секция [agent.limits]: пределы хода агента, которые задаёт чат."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    tool_result_window_share: float = Field(
        default=0.25,
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

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    always_load: Sequence[str] = Field(
        default=(),
        description=(
            "Полные имена инструментов, как их видит модель "
            "(mcp__<сервер>__<инструмент>), которые уходят модели сразу; "
            "остальные отложены — модель видит их имена и загружает "
            "инструментом ToolSearch. Пустой список — всё через ToolSearch."
        ),
    )


class AgentConfig(BaseModel):
    """Общие параметры агента: адрес вложений и ожидание ответа пользователя."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    public_url: str | None = Field(default=None, min_length=1)
    question_timeout_sec: int = Field(default=3600, gt=0)


class ProcessAgentConfig(AgentConfig, AgentCommandConfig):
    """Локальный процесс: команда, история и настройки хода принадлежат AID."""

    mode: Literal["process"] = "process"
    history: AgentHistoryConfig
    tools: AgentToolsConfig = Field(default_factory=AgentToolsConfig)
    limits: AgentLimitsConfig = Field(default_factory=AgentLimitsConfig)


class ServiceAgentConfig(AgentConfig, AgentServiceConfig):
    """Удалённый агент: endpoint и авторизация; история находится у службы."""

    mode: Literal["service"] = "service"


AgentSettings = Annotated[
    ProcessAgentConfig | ServiceAgentConfig, Field(discriminator="mode")
]


class SessionConfig(SessionPolicy):
    """Секция [session] приложения с браузером: к токену сессии добавлена
    cookie входа.
    """

    auth_secret: str = Field(
        min_length=1,
        description="Секрет подписи JWT и печати credentials.",
    )

    cookie: str = Field(min_length=1, description="Имя cookie входа.")
    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        description="SameSite cookie входа; none включает Secure."
    )

    def cookie_spec(self) -> CookieSpec:
        return CookieSpec(
            name=self.cookie,
            samesite=self.cookie_samesite,
            ttl_sec=self.session_ttl_sec,
        )

    def renewal(self) -> SessionRenewal:
        return SessionRenewal.of(self.session_ttl_sec, self.session_max_sec)


class McpDefaultsConfig(BaseModel):
    """Сроки новых MCP-серверов; форма копирует их в запись сервера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_timeout_sec: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    call_timeout_sec: float = Field(default=60.0, gt=0, allow_inf_nan=False)


class McpServersConfig(McpDefaultsConfig, ValueCipherConfig):
    """Шифрование каталога MCP-серверов и сроки новых подключений."""

    SECTION: ClassVar[str] = "mcp_servers"


class AgentProviderKind(StrEnum):
    """Виды провайдеров модели, которые чат отдаёт агенту: удалённые по http."""

    OPENAI = "openai"
    OLLAMA = "ollama"


class FileModel(BaseModel):
    """Основа моделей файла настроек агента: ключи файла — camelCase по
    договору запуска, чат строит модели именами полей. document() даёт дамп
    ключами файла с открытыми секретами: обычный дамп пишет SecretStr
    звёздочками, под ключом контекста SecretReveal поле отдаёт значение."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    def document(self) -> dict[str, object]:
        dump = self.model_dump(
            mode="json", by_alias=True, context={SecretReveal.REVEAL_CONTEXT: True}
        )
        SecretReveal.apply(self, dump)

        return dump


class ProviderFile(FileModel):
    """Ключ `provider` профиля модели: вид, адрес, вход соединения провайдера
    и поведение запросов — срок, проверка TLS, повторы и таблица транспорта
    процесса ключами HttpTransportConfig."""

    kind: AgentProviderKind
    base_url: str = Field(min_length=1)
    auth: WebAuth
    timeout_sec: float = Field(gt=0)
    ssl_verify: bool
    retry_attempts: int = Field(ge=1)
    retry_backoff_sec: float = Field(ge=0)
    retry_statuses: Mapping[int, int]
    retry_after_max_sec: float = Field(ge=0)
    transport: Mapping[str, Any]


class ModelFile(FileModel):
    """Ключ `models.<имя>`: профиль модели агента, собранный из профиля чата
    и его пределов модели."""

    provider: ProviderFile
    model: str = Field(min_length=1)
    sampling: Mapping[str, Any]
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    reasoning_return: ReasoningReturn


class PermissionsFile(FileModel):
    """Ключ `permissions`: потолок режимов разрешений процесса — право
    включить bypassPermissions. Сам режим сессии идёт по ACP."""

    allow_bypass: bool


class HistoryPoolFile(FileModel):
    """Ключ `history.pool`: пул соединений истории процесса агента — из
    пула подключения истории [agent.history.postgres.pool]."""

    min_size: int = Field(ge=1)
    max_size: int = Field(ge=1)
    timeout_sec: float = Field(gt=0)


class PostgresHistoryFile(FileModel):
    """Ключ `history` вида postgres: строка libpq, схема журнала и пул."""

    kind: Literal[AgentHistoryKind.POSTGRES]
    dsn: SecretStr
    db_schema: str = Field(alias="schema", min_length=1)
    pool: HistoryPoolFile


class MemoryHistoryFile(FileModel):
    """Ключ `history` вида memory: журнал только в памяти процесса."""

    kind: Literal[AgentHistoryKind.MEMORY]


class McpFile(FileModel):
    """Ключ `mcp`: срок одного вызова tools/call серверов сессии."""

    call_timeout_sec: float = Field(gt=0)


class ToolsFile(FileModel):
    """Ключ `tools`: инструменты, которые модель получает полным определением
    с первого запроса, — из [agent.tools]; остальные отложены до ToolSearch."""

    always_load: Sequence[str]


class LimitsFile(FileModel):
    """Ключ `limits`: пределы хода из [agent.limits]."""

    tool_result_window_share: float


class SettingsFile(FileModel):
    """Файл настроек процесса агента целиком, как его пишет чат."""

    model: str = Field(min_length=1)
    models: Mapping[str, ModelFile]
    permissions: PermissionsFile
    history: PostgresHistoryFile | MemoryHistoryFile = Field(discriminator="kind")
    mcp: McpFile | None
    tools: ToolsFile
    limits: LimitsFile
    env: Mapping[str, str]
    system_prompt_file: str = Field(min_length=1)


class RemoteProviderView(BaseModel):
    """Удалённый провайдер профиля чата глазами агента: вид, соединение и
    транспорт процесса."""

    model_config = ConfigDict(extra="ignore")

    kind: AgentProviderKind
    connection: HttpConnection
    transport: HttpTransportConfig


class LocalMessagingConfig(MessagingConfig):
    """Настройки обмена в памяти одного процесса."""

    provider: Literal["local"] = "local"


class PostgresMessagingConfig(MessagingConfig):
    """Настройки обмена через таблицы и LISTEN/NOTIFY PostgreSQL."""

    provider: Literal["postgres"] = "postgres"


class AidSection(ServerConfig):
    """Секция [server]: HTTP-сервер чата — адрес, TLS, префикс приложения,
    пульс сокетов, остановка uvicorn и песочница приложений MCP Apps. Модель
    собирает AppConfig; bootstrap поднимает по ней uvicorn и ставит префикс,
    AidListeners открывает её сокеты. Статику chainlit точка входа берёт из
    share/ каталога программы, вложения — из files/ каталога данных
    экземпляра."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    port: int = Field(default=8501, gt=0, lt=65536)
    ssl_cert: str | None = Field(default=None)
    ssl_key: str | None = Field(default=None)
    ssl_ca_certs: str | None = Field(default=None)

    sandbox_port: int = Field(
        gt=0,
        le=65535,
        description=(
            "Порт страницы-прокси песочницы приложений MCP Apps: одна на "
            "процесс, открывается при старте. Адрес её страницы в браузере — "
            "схема и хост public_url с этим портом."
        ),
    )

    ping_interval: int = Field(
        default=300,
        description=(
            "Период heartbeat'а engine.io, секунды: с ним сервер шлёт клиенту "
            "ping, значение уезжает клиенту в handshake. Клиент считает "
            "соединение мёртвым, если ping не пришёл за ping_interval + "
            "ping_timeout, и переподключается; большое значение даёт пережить "
            "паузу на breakpoint'е отладки."
        ),
    )

    ping_timeout: int = Field(
        default=300,
        description=(
            "Ожидание pong'а engine.io, секунды: без ответа сервер закрывает "
            "сессию сокета с причиной ping timeout. Вместе с ping_interval "
            "задаёт, через сколько тишины вкладка уходит в реконнект."
        ),
    )

    max_decode_packets: int = Field(
        default=256,
        description=(
            "за паузу на брейкпоинте клиент копит события и шлёт их одним "
            "polling-POST; дефолтных 16 пакетов не хватает"
        ),
    )

    ws_ping_interval: float = Field(
        default=20,
        ge=0,
        description=(
            "Период ws-пинга uvicorn, секунды: кадр ping самого протокола "
            "WebSocket, отдельный от heartbeat'а engine.io. Отвечает на него "
            "браузер, а не приложение. 0 — не пинговать, живость остаётся "
            "только на engine.io."
        ),
    )

    ws_ping_timeout: float = Field(
        default=20,
        ge=0,
        description=(
            "Ожидание ws-pong'а, секунды: без ответа uvicorn рвёт соединение "
            "и вкладка уходит в реконнект, даже когда ход жив. Малое значение "
            "рвёт связь на замершей вкладке и сетевых задержках. 0 — не ждать."
        ),
    )

    ws_per_message_deflate: bool = Field(
        default=True,
        description="Сжатие WebSocket-фреймов (permessage-deflate) в uvicorn.",
    )

    ws_protocol: str = Field(
        default="auto",
        description="WebSocket-реализация uvicorn: auto/websockets/wsproto/none.",
    )

    def sandbox_url(self) -> str:
        """Адрес страницы-прокси песочницы в браузере: схема и хост страницы
        чата, порт песочницы, корень пути."""
        public = HttpUrl(self.public_url)
        host = public.host
        if host is None:
            msg = f"[server].public_url {self.public_url} names no host"
            raise ValueError(msg)

        sandbox = HttpUrl.build(scheme=public.scheme, host=host, port=self.sandbox_port)

        return str(sandbox)

    def sandbox_origin(self) -> str:
        """Источник страницы-прокси песочницы в форме браузера."""
        return self._origin(HttpUrl(self.sandbox_url()))

    def chat_origin(self) -> str:
        """Источник страницы чата в форме браузера: единственный, кому
        разрешено встраивать песочницу."""
        return self._origin(HttpUrl(self.public_url))

    @staticmethod
    def _origin(url: HttpUrl) -> str:
        """Схема, хост и порт, если он не порт схемы по умолчанию; учётные
        данные адреса в источник не входят."""
        host = url.host
        if host is None:
            msg = f"[server]: address {url} expects a host, got none"
            raise ValueError(msg)

        bare = HttpUrl.build(scheme=url.scheme, host=host, port=url.port)
        parts = urlsplit(str(bare))

        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
