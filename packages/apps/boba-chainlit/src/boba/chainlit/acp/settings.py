"""Настройки агента: модели файла настроек, слои пользователь → чат в Postgres,
сборка файла из слоя чата и приватный каталог процесса.

Файл настроек — способ передачи: главная копия слоя чата лежит в таблице
`agent_chat_settings` схемы чата, слой пользователя — в `agent_user_settings`;
при первом открытии чата его слой копируется из пользовательского и дальше живёт
своей жизнью. Секреты в таблицах шифруются SecretCipher, в файле лежат открыто
под правами 0600.

Ошибки:
AgentSettingsError — профиль чата нельзя превратить в профиль модели агента.
DataUnavailableError — таблицы настроек недоступны (PgTable).
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
    SerializationInfo,
    ValidationError,
    field_serializer,
)
from pydantic.alias_generators import to_camel

from boba.chainlit.acp.config import (
    AgentConfig,
    AgentHistoryConfig,
    AgentHistoryKind,
    MemoryAgentHistory,
    PostgresAgentHistory,
)
from boba.chat.profiles import ChatProfileConfig
from boba.connections.secrets import SecretCipher
from boba.db.postgres import PostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspace
from boba.krb.credentials import ClientCredentials, KerberosEnv
from boba.runtime.table import PgTable
from boba.transport.http.connection import BearerAuth, HttpConnection

__all__ = [
    "AgentModel",
    "AgentProvider",
    "AgentSettingsError",
    "AgentSettingsFile",
    "AgentSettingsLayers",
    "AgentSettingsTable",
    "AgentWorkdir",
    "AgentWorkdirs",
    "AuthHeader",
    "ChatAgentSettings",
    "HistoryEnvironment",
    "HookMatchers",
    "ProfileModelSpecs",
    "ProviderKindName",
    "SettingsFileBuilder",
    "UserAgentSettings",
    "WorkdirFile",
]


class AgentSettingsError(Exception):
    """Профиль чата не складывается в профиль модели агента."""


class ProviderKindName(StrEnum):
    """Виды удалённых провайдеров модели, которые понимает файл агента."""

    OPENAI = "openai"
    OLLAMA = "ollama"


class PermissionModeName(StrEnum):
    """Режимы разрешений агента, которые выставляет чат."""

    BYPASS = "bypassPermissions"


class HookEventName(StrEnum):
    """События хуков агента, которые чат подключает."""

    PRE_TOOL_USE = "PreToolUse"


class HookTypeName(StrEnum):
    """Виды обработчиков хука."""

    HTTP = "http"


class HookFailureName(StrEnum):
    """Что делает агент, когда обработчик хука не ответил."""

    BLOCK = "block"
    CONTINUE = "continue"


class AuthHeader(StrEnum):
    """Заголовок Authorization с токеном носителя."""

    NAME = "Authorization"
    TEMPLATE = "Bearer {token}"

    @classmethod
    def render(cls, token: str) -> str:
        return cls.TEMPLATE.value.format(token=token)


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


class SettingsFileModel(BaseModel):
    """Основа моделей файла агента: ключи camelCase, лишние ключи — ошибка."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )


class AgentProvider(SettingsFileModel):
    """Провайдер профиля модели: вид, адрес API и ключ."""

    kind: ProviderKindName
    base_url: str = Field(min_length=1)
    api_key: SecretStr | None = None

    @field_serializer("api_key", when_used="json")
    def _reveal_api_key(
        self, value: SecretStr | None, info: SerializationInfo
    ) -> str | None:
        """Файл настроек — единственное место, где ключ лежит открыто."""
        if value is None:
            return None

        return value.get_secret_value()


class AgentModel(SettingsFileModel):
    """Профиль модели агента: провайдер, имя модели, сэмплинг и пределы."""

    provider: AgentProvider
    model: str = Field(min_length=1)
    sampling: Mapping[str, Any]
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class AgentPermissions(SettingsFileModel):
    """Режим разрешений: чат пока пропускает все действия без вопросов."""

    default_mode: PermissionModeName
    allow_bypass: bool


class HistoryFile(SettingsFileModel):
    """Секция history файла: Postgres со строкой подключения либо память."""

    kind: AgentHistoryKind
    dsn: SecretStr | None = None
    db_schema: str | None = Field(default=None, alias="schema")

    @field_serializer("dsn", when_used="json")
    def _reveal_dsn(
        self, value: SecretStr | None, info: SerializationInfo
    ) -> str | None:
        if value is None:
            return None

        return value.get_secret_value()


class HttpHookFile(SettingsFileModel):
    """Обработчик хука типа http."""

    type: Literal[HookTypeName.HTTP]
    url: str = Field(min_length=1)
    headers: Mapping[str, str]
    timeout: float = Field(gt=0)
    on_failure: HookFailureName


class HookGroupFile(SettingsFileModel):
    """Группа обработчиков под одним матчером."""

    matcher: str
    hooks: Sequence[HttpHookFile] = Field(min_length=1)


class AgentSettingsFile(SettingsFileModel):
    """Файл настроек агента целиком: то, что уходит в `--settings`."""

    model: str = Field(min_length=1)
    models: Mapping[str, AgentModel]
    permissions: AgentPermissions
    history: HistoryFile
    hooks: Mapping[HookEventName, Sequence[HookGroupFile]]
    system_prompt_file: str = Field(min_length=1)

    def render(self) -> str:
        """JSON файла с раскрытыми секретами."""
        document = self.model_dump(mode="json", by_alias=True, exclude_none=True)

        return json.dumps(document, ensure_ascii=False, indent=2)


class UserAgentSettings(BaseModel):
    """Слой пользователя: что он сохранил сам поверх профиля чата; None —
    поле не переопределено. Ввод в панели — W11, здесь модель и хранение."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str | None = None
    system_prompt: str | None = None


class ChatAgentSettings(BaseModel):
    """Слой чата: профиль модели агента и системный промпт одного треда."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str = Field(min_length=1)
    model: AgentModel
    system_prompt: str

    def with_user(self, user: UserAgentSettings) -> ChatAgentSettings:
        """Слой чата с полями, которые пользователь переопределил."""
        provider = self.model.provider
        if user.base_url is not None:
            provider = provider.model_copy(update={"base_url": user.base_url})

        if user.api_key is not None:
            provider = provider.model_copy(update={"api_key": user.api_key})

        model = self.model.model_copy(update={"provider": provider})
        if user.model is not None:
            model = model.model_copy(update={"model": user.model})

        system_prompt = self.system_prompt
        if user.system_prompt is not None:
            system_prompt = user.system_prompt

        return ChatAgentSettings(
            model_name=self.model_name, model=model, system_prompt=system_prompt
        )


class RemoteProviderView(BaseModel):
    """Удалённый провайдер профиля чата глазами агента: вид и соединение."""

    model_config = ConfigDict(extra="ignore")

    kind: ProviderKindName
    connection: HttpConnection


class ProfileModelSpecs:
    """Профиль модели агента из профиля чата: адрес и ключ — из соединения
    провайдера, пределы — из конфига [agent.limits]."""

    def __init__(self, context_window: int, max_output_tokens: int) -> None:
        self._context_window = context_window
        self._max_output_tokens = max_output_tokens

    def chat_settings(self, name: str, profile: ChatProfileConfig) -> ChatAgentSettings:
        """Ошибки:
        AgentSettingsError — провайдер профиля не удалённый или без адреса.
        """
        return ChatAgentSettings(
            model_name=name,
            model=self.model(name, profile),
            system_prompt=profile.system_prompt,
        )

    def model(self, name: str, profile: ChatProfileConfig) -> AgentModel:
        """Ошибки:
        AgentSettingsError — провайдер профиля не удалённый или без адреса.
        """
        try:
            view = RemoteProviderView.model_validate(profile.provider.model_dump())
        except ValidationError as exc:
            msg = (
                f"chat profile {name!r}: provider kind {profile.provider.kind!r} "
                "cannot be handed to the agent, expected a remote provider "
                f"(openai or ollama) with an http connection: {exc}"
            )
            raise AgentSettingsError(msg) from exc

        api_key = None
        if isinstance(view.connection.auth, BearerAuth):
            api_key = view.connection.auth.token

        provider = AgentProvider(
            kind=view.kind,
            base_url=str(view.connection.public_url()),
            api_key=api_key,
        )

        return AgentModel(
            provider=provider,
            model=profile.model,
            sampling=dict(profile.sampling),
            context_window=self._context_window,
            max_output_tokens=self._max_output_tokens,
        )


class SettingsTable(StrEnum):
    """Таблицы слоёв настроек агента в схеме чата."""

    USERS = "agent_user_settings"
    CHATS = "agent_chat_settings"


class SettingsColumn(StrEnum):
    """Колонки таблиц настроек."""

    USER_ID = "user_id"
    THREAD_ID = "thread_id"
    PROFILE = "profile"
    SETTINGS = "settings"
    UPDATED_AT = "updated_at"


class AgentSettingsTable(PgTable):
    """Слои настроек агента в Postgres чата: строка пользователя на профиль и
    строка чата на тред; секреты строк зашифрованы."""

    def __init__(
        self,
        postgres: PostgresConfig,
        db_schema: str,
        cipher: SecretCipher,
        pool: PostgresPool | None = None,
    ) -> None:
        super().__init__(postgres, db_schema, pool)
        self._cipher = cipher

    async def setup(self) -> None:
        ddl = (
            self._query()
            .add(
                """
                create table if not exists {schema}.agent_user_settings (
                    user_id    uuid not null,
                    profile    text not null,
                    settings   jsonb not null,
                    updated_at timestamptz not null default now(),
                    primary key (user_id, profile)
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.agent_chat_settings (
                    thread_id  uuid primary key,
                    user_id    uuid not null,
                    profile    text not null,
                    settings   jsonb not null,
                    updated_at timestamptz not null default now()
                )
                """
            )
            .build(),
        )

        await self._apply_ddl(ddl)

    async def user_layer(self, user_id: UUID, profile: str) -> UserAgentSettings:
        """Слой пользователя; без строки — ничего не переопределено."""
        query = (
            self._query()
            .add(
                """
                select settings
                from {schema}.agent_user_settings
                where user_id = %(user_id)s and profile = %(profile)s
                """,
                user_id=user_id,
                profile=profile,
            )
            .build()
        )
        row = await self._row(query, "agent user settings")
        if row is None:
            return UserAgentSettings()

        return self._parse(
            UserAgentSettings, self._cipher.decrypt(row[SettingsColumn.SETTINGS.value])
        )

    async def save_user_layer(
        self, user_id: UUID, profile: str, settings: UserAgentSettings
    ) -> None:
        query = (
            self._query()
            .add(
                """
                insert into {schema}.agent_user_settings (user_id, profile, settings)
                values (%(user_id)s, %(profile)s, %(settings)s)
                on conflict (user_id, profile) do update set
                    settings = excluded.settings,
                    updated_at = now()
                """,
                user_id=user_id,
                profile=profile,
                settings=Jsonb(self._cipher.encrypt(settings)),
            )
            .build()
        )

        await self._execute(query, "save agent user settings")

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


class AgentSettingsLayers:
    """Слои настроек агента для треда: вход пользователя → профиль чата и
    сохранённые им поля → слой чата, скопированный при первом открытии.

    Создаётся провайдером процесса; зовут объекты треда перед запуском агента.
    """

    def __init__(self, table: AgentSettingsTable, specs: ProfileModelSpecs) -> None:
        self._table = table
        self._specs = specs

    async def user_layer(
        self, user_id: UUID, name: str, profile: ChatProfileConfig
    ) -> ChatAgentSettings:
        """Слой пользователя как готовые настройки чата: профиль плюс его поля.

        Ошибки:
        AgentSettingsError — провайдер профиля не годится агенту.
        """
        base = self._specs.chat_settings(name, profile)
        saved = await self._table.user_layer(user_id, name)

        return base.with_user(saved)

    async def chat_layer(
        self, thread_id: UUID, user_id: UUID, name: str, profile: ChatProfileConfig
    ) -> ChatAgentSettings:
        """Слой чата треда; у нового треда он копируется из пользовательского.

        Ошибки:
        AgentSettingsError — провайдер профиля не годится агенту.
        """
        existing = await self._table.chat_layer(thread_id)
        if existing is not None:
            return existing

        copied = await self.user_layer(user_id, name, profile)
        await self._table.save_chat_layer(thread_id, user_id, name, copied)

        return copied


class HookMatchers:
    """Матчер хука по именам серверов MCP: `mcp__<server>__<tool>` без разбора
    имён инструментов."""

    TEMPLATE: ClassVar[str] = "^mcp__{server}__"

    def of(self, servers: Sequence[str]) -> str:
        parts: list[str] = []
        for name in servers:
            parts.append(self.TEMPLATE.format(server=name))

        return "|".join(parts)


class SettingsFileBuilder:
    """Файл настроек агента из слоя чата и конфига [agent]: история, режим
    разрешений и хук добавляются к профилю модели чата."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._matchers = HookMatchers()

    def build(
        self, chat: ChatAgentSettings, user_jwt: str, hooked_servers: Sequence[str]
    ) -> AgentSettingsFile:
        return AgentSettingsFile(
            model=chat.model_name,
            models={chat.model_name: chat.model},
            permissions=AgentPermissions(
                default_mode=PermissionModeName.BYPASS, allow_bypass=True
            ),
            history=self._history(),
            hooks=self._hooks(user_jwt, hooked_servers),
            system_prompt_file=WorkdirFile.SYSTEM_PROMPT.value,
        )

    def _history(self) -> HistoryFile:
        history = self._config.history
        if isinstance(history, PostgresAgentHistory):
            return HistoryFile.model_validate(
                {
                    "kind": AgentHistoryKind.POSTGRES,
                    "dsn": history.dsn(),
                    "schema": history.db_schema,
                }
            )

        if isinstance(history, MemoryAgentHistory):
            return HistoryFile(kind=AgentHistoryKind.MEMORY)

        msg = f"[agent.history] of kind {history.kind!r} has no file form"
        raise AgentSettingsError(msg)

    def _hooks(
        self, user_jwt: str, hooked_servers: Sequence[str]
    ) -> dict[HookEventName, Sequence[HookGroupFile]]:
        hook = self._config.hook
        if not hook.enable:
            return {}

        if not hooked_servers:
            return {}

        handler = HttpHookFile(
            type=HookTypeName.HTTP,
            url=self._config.public_url.rstrip("/") + hook.path,
            headers={AuthHeader.NAME.value: AuthHeader.render(user_jwt)},
            timeout=hook.timeout_sec,
            on_failure=HookFailureName.BLOCK,
        )
        group = HookGroupFile(
            matcher=self._matchers.of(hooked_servers), hooks=[handler]
        )

        return {HookEventName.PRE_TOOL_USE: [group]}


class HistoryEnvironment:
    """Окружение libkrb5 для процесса агента с kerberos-ролью истории: путь к
    кэшу билета и к krb5.conf рабочего каталога chainlit; билет в кэше
    обновляется по keytab до запуска. У парольной роли окружение пустое.

    Исключение §17: libpq читает KRB5CCNAME/KRB5_CONFIG из окружения сам;
    это пути к файлам того же пользователя, не секреты.
    """

    def __init__(self, history: AgentHistoryConfig) -> None:
        self._history = history

    async def prepare(self) -> dict[str, str]:
        """Ошибки:
        KerberosError — билет по keytab не получен.
        """
        if not isinstance(self._history, PostgresAgentHistory):
            return {}

        auth = self._history.kerberos_auth()
        if auth is None:
            return {}

        credentials = ClientCredentials.of(auth)
        await credentials.ensure_async()

        return {
            KerberosEnv.CCACHE: KerberosWorkspace.ccache_for(auth),
            KerberosEnv.CONFIG: KerberosWorkspace.krb5_config(),
        }


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

    def write(self, file: AgentSettingsFile, system_prompt: str) -> None:
        """Файл настроек и промпт с правами 0600."""
        self._write_private(self.settings_path, file.render())
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
