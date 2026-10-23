"""Настройки агента: модель файла настроек глазами чата, слой чата в
Postgres, сборка файла из слоя чата и приватный каталог процесса.

Файл настроек — договор запуска агента: чат пишет в него то, чему в ACP
нет пути, — провайдера и модель профиля, системный промпт, хранилище
истории, сроки серверов MCP, политику запуска `permissions.allowBypass` и
окружение libkrb5. Схема файла принадлежит агенту и публикуется им командой
`boba settings-schema`; модели этого модуля — собственная запись чата по тем
же ключам (camelCase), её сверяет со схемой контрактный тест. Режим
разрешений и профиль модели сессии чат выставляет по ACP (`configOptions`,
`session/set_config_option`), серверы MCP объявляет в методах сессии.

Слой чата собирается из профиля чата и конфига [agent] при каждом запуске
агента треда и сохраняется в таблицу `agent_chat_settings` схемы чата: по нему
удаление треда поднимает короткий процесс `session/delete`. Пользователь
настроек агента не меняет. Секреты в таблице шифруются SecretCipher, в файле
лежат открыто под правами 0600.

Ошибки:
AgentSettingsError — профиль чата нельзя превратить в профиль модели агента.
DataUnavailableError — таблица настроек недоступна (PgTable).
PostgresError — билет kerberos-роли истории не получен.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)
from pydantic.alias_generators import to_camel

from boba.aid.agent.config import (
    AgentConfig,
    AgentHistoryConfig,
    AgentHistoryKind,
    MemoryAgentHistory,
    PostgresAgentHistory,
)
from boba.chat.profiles import ChatProfileConfig, ReasoningReturn
from boba.connections.secrets import SecretCipher
from boba.core.secrets import SecretReveal
from boba.db.postgres import PostgresPool
from boba.db.postgres.connection import LibpqConnection, PostgresConfig
from boba.mcp_client.client import McpServersConfig
from boba.runtime.table import PgTable
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import BearerAuth, HttpConnection

__all__ = [
    "AgentOwner",
    "AgentProviderKind",
    "AgentSettingsError",
    "AgentSettingsTable",
    "AgentWorkdir",
    "AgentWorkdirs",
    "ChatAgentSettings",
    "HistoryEnvironment",
    "McpFile",
    "MemoryHistoryFile",
    "ModelFile",
    "PermissionsFile",
    "PostgresHistoryFile",
    "ProfileModelSpecs",
    "ProviderFile",
    "SettingsFile",
    "SettingsFileBuilder",
    "WorkdirFile",
]


class AgentSettingsError(Exception):
    """Профиль чата не складывается в профиль модели агента."""


class WorkdirFile(StrEnum):
    """Файлы приватного каталога процесса агента."""

    SETTINGS = "settings.json"
    SYSTEM_PROMPT = "system_prompt.txt"
    STDERR = "agent.log"
    ENCODING = "utf-8"


class PrivateMode(IntEnum):
    """Права приватного каталога и его файлов."""

    DIR = 0o700
    FILE = 0o600


class AgentProviderKind(StrEnum):
    """Виды провайдеров модели, которые чат отдаёт агенту: удалённые по http."""

    OPENAI = "openai"
    OLLAMA = "ollama"


class FileModel(BaseModel):
    """Основа моделей файла настроек агента: ключи файла — camelCase по
    договору запуска, имена полей тоже принимаются (так читается слой чата
    из таблицы), лишние ключи сохранённой строки отбрасываются. document()
    даёт дамп ключами файла с открытыми секретами: обычный дамп пишет
    SecretStr звёздочками, под ключом контекста SecretReveal поле отдаёт
    значение."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )

    def document(self) -> dict[str, object]:
        dump = self.model_dump(
            mode="json", by_alias=True, context={SecretReveal.REVEAL_CONTEXT: True}
        )
        SecretReveal.apply(self, dump)

        return dump


class ProviderFile(FileModel):
    """Ключ `provider` профиля модели: вид, адрес, ключ и поведение запросов
    — срок, проверка TLS, повторы и таблица транспорта процесса ключами
    HttpTransportConfig."""

    kind: AgentProviderKind
    base_url: str = Field(min_length=1)
    api_key: SecretStr | None = None
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


class PostgresHistoryFile(FileModel):
    """Ключ `history` вида postgres: строка libpq и схема журнала."""

    kind: Literal[AgentHistoryKind.POSTGRES]
    dsn: SecretStr
    db_schema: str = Field(alias="schema", min_length=1)


class MemoryHistoryFile(FileModel):
    """Ключ `history` вида memory: журнал только в памяти процесса."""

    kind: Literal[AgentHistoryKind.MEMORY]


class McpFile(FileModel):
    """Ключ `mcp`: срок одного вызова tools/call серверов сессии."""

    call_timeout_sec: float = Field(gt=0)


class SettingsFile(FileModel):
    """Файл настроек процесса агента целиком, как его пишет чат."""

    model: str = Field(min_length=1)
    models: Mapping[str, ModelFile]
    permissions: PermissionsFile
    history: PostgresHistoryFile | MemoryHistoryFile = Field(discriminator="kind")
    mcp: McpFile | None
    env: Mapping[str, str]
    system_prompt_file: str = Field(min_length=1)


class AgentOwner(BaseModel):
    """Пользователь треда глазами службы агента: логин и роли, с которыми
    чат входит в службу от его имени (вход proxy)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    roles: tuple[str, ...]


class ChatAgentSettings(BaseModel):
    """Слой чата: профиль модели агента и системный промпт профиля одного
    треда и его пользователь. Из него собирается файл настроек процесса, а в
    режиме службы — вход в неё при удалении треда; лишние ключи сохранённой
    строки при чтении отбрасываются. owner None — строка записана до режима
    службы: процессу владелец не нужен."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_name: str = Field(min_length=1)
    model: ModelFile
    system_prompt: str
    owner: AgentOwner | None = None


class RemoteProviderView(BaseModel):
    """Удалённый провайдер профиля чата глазами агента: вид, соединение и
    транспорт процесса."""

    model_config = ConfigDict(extra="ignore")

    kind: AgentProviderKind
    connection: HttpConnection
    transport: HttpTransportConfig


class ProfileModelSpecs:
    """Профиль модели агента из профиля чата: адрес, ключ, срок, TLS и
    повторы — из соединения провайдера, транспорт — из его секции
    ([agent_http]), окно, потолок ответа и возврат рассуждений — из пределов
    профиля ([model_limits.<имя>]).

    Создаёт его запуск агента (AgentLauncher)."""

    def chat_settings(
        self, name: str, profile: ChatProfileConfig, owner: AgentOwner
    ) -> ChatAgentSettings:
        """Ошибки:
        AgentSettingsError — провайдер профиля не удалённый по http или без адреса.
        """
        return ChatAgentSettings(
            model_name=name,
            model=self.model(name, profile),
            system_prompt=profile.system_prompt,
            owner=owner,
        )

    def model(self, name: str, profile: ChatProfileConfig) -> ModelFile:
        """Ошибки:
        AgentSettingsError — провайдер профиля не удалённый или без адреса.
        """
        remote: list[str] = []
        for kind in AgentProviderKind:
            remote.append(kind.value)

        try:
            view = RemoteProviderView.model_validate(profile.provider.model_dump())
        except ValidationError as exc:
            msg = (
                f"chat profile {name!r}: provider kind {profile.provider.kind!r} "
                "cannot be handed to the agent, expected a remote provider "
                f"({remote}) with an http connection: {exc}"
            )
            raise AgentSettingsError(msg) from exc

        base_url = str(view.connection.public_url())
        if not base_url:
            msg = f"chat profile {name!r}: the provider connection gives no address"
            raise AgentSettingsError(msg)

        api_key = None
        if isinstance(view.connection.auth, BearerAuth):
            api_key = view.connection.auth.token

        connection = view.connection
        provider = ProviderFile(
            kind=view.kind,
            base_url=base_url,
            api_key=api_key,
            timeout_sec=connection.timeout_sec,
            ssl_verify=connection.ssl_verify,
            retry_attempts=connection.retry_attempts,
            retry_backoff_sec=connection.retry_backoff_sec,
            retry_statuses=connection.retry_statuses.root,
            retry_after_max_sec=connection.retry_after_max_sec,
            transport=view.transport.model_dump(mode="json"),
        )

        return ModelFile(
            provider=provider,
            model=profile.model,
            sampling=dict(profile.sampling),
            context_window=profile.limits.context_window,
            max_output_tokens=profile.limits.max_output_tokens,
            reasoning_return=profile.limits.reasoning_return,
        )


class SettingsColumn(StrEnum):
    """Колонки таблиц настроек."""

    USER_ID = "user_id"
    THREAD_ID = "thread_id"
    PROFILE = "profile"
    SETTINGS = "settings"
    UPDATED_AT = "updated_at"


class AgentSettingsTable(PgTable):
    """Слой чата настроек агента в Postgres чата: строка на тред, секреты
    строки зашифрованы. Пишет её запуск агента (AgentLauncher), читает и
    стирает удаление треда (ThreadDeleter)."""

    def __init__(
        self,
        postgres: PostgresConfig,
        db_schema: str,
        cipher: SecretCipher,
        pool: PostgresPool | None = None,
    ) -> None:
        super().__init__(postgres, db_schema, pool)
        self._cipher = cipher

    async def chat_layer(self, thread_id: UUID) -> ChatAgentSettings | None:
        """Слой чата; None — у треда ещё нет своего слоя."""
        query = (
            self._query()
            .add(
                """
                select settings
                from {schema}.agent_chat_settings
                where thread_id = %(thread_id)s
                """,
                thread_id=thread_id,
            )
            .build()
        )
        row = await self._row(query, "agent chat settings")
        if row is None:
            return None

        return self._parse(
            ChatAgentSettings, self._cipher.decrypt(row[SettingsColumn.SETTINGS.value])
        )

    async def save_chat_layer(
        self, thread_id: UUID, user_id: UUID, profile: str, settings: ChatAgentSettings
    ) -> None:
        query = (
            self._query()
            .add(
                """
                insert into {schema}.agent_chat_settings
                    (thread_id, user_id, profile, settings)
                values (%(thread_id)s, %(user_id)s, %(profile)s, %(settings)s)
                on conflict (thread_id) do update set
                    settings = excluded.settings,
                    updated_at = now()
                """,
                thread_id=thread_id,
                user_id=user_id,
                profile=profile,
                settings=Jsonb(self._cipher.encrypt(settings)),
            )
            .build()
        )

        await self._execute(query, "save agent chat settings")

    async def delete_chat_layer(self, thread_id: UUID) -> None:
        query = (
            self._query()
            .add(
                """
                delete from {schema}.agent_chat_settings
                where thread_id = %(thread_id)s
                """,
                thread_id=thread_id,
            )
            .build()
        )

        await self._execute(query, "delete agent chat settings")


class SettingsFileBuilder:
    """Файл настроек агента из слоя чата и конфига: к профилю модели чата
    добавляются политика запуска `allowBypass` (режим сессии чат выставляет
    по ACP), история, срок вызова инструментов — наибольший
    `limits.call_timeout_sec` серверов профиля [mcp.servers.*] — и окружение
    libkrb5 истории ключом `env`. Строка подключения истории — её собственное
    подключение [agent.history].postgres. Файл — единственный канал настроек
    запуска агента: argv называет только его, окружение ребёнка — минимальный
    набор SDK."""

    def __init__(self, config: AgentConfig, servers: McpServersConfig) -> None:
        self._servers = servers
        self._history = self._history_file(config.history)

    def build(
        self,
        chat: ChatAgentSettings,
        mcp: McpFile | None,
        env: Mapping[str, str],
    ) -> SettingsFile:
        """Файл процесса агента по слою чата; mcp None — процесс не зовёт
        инструментов серверов (короткий процесс удаления сессии)."""
        return SettingsFile(
            model=chat.model_name,
            models={chat.model_name: chat.model},
            permissions=PermissionsFile(allow_bypass=True),
            history=self._history,
            mcp=mcp,
            env=env,
            system_prompt_file=WorkdirFile.SYSTEM_PROMPT.value,
        )

    def mcp(self, names: Sequence[str]) -> McpFile | None:
        """Сроки серверов MCP агента: вызов не обрывается раньше, чем его
        оборвал бы ретранслятор чата у самого терпеливого сервера профиля;
        None — у профиля нет серверов [mcp.servers.*].

        Ошибки:
        AgentSettingsError — профиль называет сервер, которого нет в [mcp.servers].
        """
        limits: list[float] = []
        for name in names:
            server = self._servers.servers.get(name)
            if server is None:
                msg = (
                    f"MCP server {name!r} of the profile has no section "
                    f"[mcp.servers.{name}], expected one of "
                    f"{sorted(self._servers.servers)}"
                )
                raise AgentSettingsError(msg)

            limits.append(server.limits.call_timeout_sec)

        if not limits:
            return None

        return McpFile(call_timeout_sec=max(limits))

    @staticmethod
    def _history_file(
        history: AgentHistoryConfig,
    ) -> PostgresHistoryFile | MemoryHistoryFile:
        if isinstance(history, PostgresAgentHistory):
            return PostgresHistoryFile(
                kind=AgentHistoryKind.POSTGRES,
                dsn=LibpqConnection(history.postgres).dsn(),
                schema=history.db_schema,
            )

        if isinstance(history, MemoryAgentHistory):
            return MemoryHistoryFile(kind=AgentHistoryKind.MEMORY)

        msg = f"[agent.history] of kind {history.kind!r} has no file form"
        raise AgentSettingsError(msg)


class HistoryEnvironment:
    """Окружение libkrb5 для процесса агента с kerberos-ролью истории (свой
    принципал [agent.history].postgres.auth): путь к кэшу билета и к
    krb5.conf рабочего каталога процесса aid; билет в кэше обновляется до
    запуска. У парольной роли окружение пустое.
    Едет ключом `env` файла настроек: агент ставит его себе на старте, потому
    что libpq читает KRB5CCNAME/KRB5_CONFIG из окружения сам; это пути к
    файлам того же пользователя, не секреты.
    """

    def __init__(self, history: AgentHistoryConfig) -> None:
        self._libpq: LibpqConnection | None = None
        if isinstance(history, PostgresAgentHistory):
            self._libpq = LibpqConnection(history.postgres)

    async def prepare(self) -> Mapping[str, str]:
        """Ошибки:
        PostgresError — билет роли истории не получен.
        """
        if self._libpq is None:
            return {}

        return await self._libpq.environment()


class AgentWorkdir:
    """Приватный каталог одного процесса агента: файл настроек, системный
    промпт, журнал stderr; удаляется с процессом."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def settings_path(self) -> Path:
        return self.path / WorkdirFile.SETTINGS.value

    @property
    def prompt_path(self) -> Path:
        return self.path / WorkdirFile.SYSTEM_PROMPT.value

    @property
    def stderr_path(self) -> Path:
        return self.path / WorkdirFile.STDERR.value

    def write(self, settings: SettingsFile, system_prompt: str) -> None:
        """Файл настроек ключами договора с открытыми секретами и промпт с
        правами 0600."""
        text = json.dumps(settings.document(), ensure_ascii=False, indent=2)
        self._write_private(self.settings_path, text)
        self._write_private(self.prompt_path, system_prompt)

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def exists(self) -> bool:
        return self.path.is_dir()

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PrivateMode.FILE.value
        )
        with os.fdopen(descriptor, "w", encoding=WorkdirFile.ENCODING.value) as handle:
            handle.write(text)

        os.chmod(path, PrivateMode.FILE.value)


class AgentWorkdirs:
    """Фабрика приватных каталогов под корнем [agent.process].private_dir."""

    PREFIX: ClassVar[str] = "thread-"

    def __init__(self, root: Path) -> None:
        self._root = root

    def create(self, thread_id: str) -> AgentWorkdir:
        self._root.mkdir(parents=True, exist_ok=True, mode=PrivateMode.DIR.value)
        path = Path(
            tempfile.mkdtemp(prefix=f"{self.PREFIX}{thread_id}-", dir=self._root)
        )
        os.chmod(path, PrivateMode.DIR.value)

        return AgentWorkdir(path)
