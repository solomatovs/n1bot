"""Настройки агента: модели файла настроек, слои пользователь → чат в Postgres,
сборка файла из слоя чата и приватный каталог процесса.

Файл настроек — способ передачи: главная копия слоя чата лежит в таблице
`agent_chat_settings` схемы чата, слой пользователя — в `agent_user_settings`;
при первом открытии чата его слой копируется из пользовательского и дальше живёт
своей жизнью. Правку панели в оба слоя пишет AgentSettingsLayers.change, зовёт
его AgentSettingsChanges (chat.py). Секреты в таблицах шифруются SecretCipher,
в файле лежат открыто под правами 0600.

Серверов MCP и области вызовов в файле нет: серверы процесс получает
объявлениями type "acp" в методах сессии, вызовы исполняет ретранслятор
чата (relay.py).

Ошибки:
AgentSettingsError — профиль чата нельзя превратить в профиль модели агента.
DataUnavailableError — таблицы настроек недоступны (PgTable).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)

from boba.agent.records import PermissionMode
from boba.agent.settings import (
    HistoryKind,
    MemoryHistorySettings,
    ModelSettings,
    PermissionSettings,
    PostgresHistorySettings,
    ProviderKind,
    ProviderSettings,
    SettingsLayer,
)
from boba.aid.agent.config import (
    AgentConfig,
    AgentHistoryConfig,
    LibpqConnection,
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
    "AgentSettingsChange",
    "AgentSettingsError",
    "AgentSettingsLayers",
    "AgentSettingsTable",
    "AgentWorkdir",
    "AgentWorkdirs",
    "ChatAgentSettings",
    "HistoryEnvironment",
    "ProfileModelSpecs",
    "SettingsFileBuilder",
    "UserAgentSettings",
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


class UserAgentSettings(BaseModel):
    """Слой пользователя: что он сохранил сам поверх профиля чата; None —
    поле не переопределено. Ключи строк прошлых версий (`system_prompt`)
    при чтении отбрасываются."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str | None = None


@dataclass(frozen=True)
class AgentSettingsChange:
    """Правка настроек агента из панели чата: слой пользователя целиком и
    выбор вкладки «Сессия». None у режима и модели — вкладки в форме не было,
    значение слоя чата остаётся прежним. Собирает AgentPanel.parse, применяет
    AgentSettingsLayers.change."""

    user: UserAgentSettings
    mode: PermissionMode | None
    model_name: str | None


class ChatAgentSettings(BaseModel):
    """Слой чата: профиль модели агента, системный промпт профиля и режим
    разрешений одного треда. Из него собирается файл настроек процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str = Field(min_length=1)
    model: ModelSettings
    system_prompt: str
    mode: PermissionMode = PermissionMode.BYPASS_PERMISSIONS

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

        return self.model_copy(update={"model": model})

    def with_session(
        self, mode: PermissionMode | None, model_name: str | None
    ) -> ChatAgentSettings:
        """Слой чата с выбором вкладки «Сессия»: режим разрешений и имя
        профиля модели по умолчанию; None — значение остаётся прежним."""
        update: dict[str, Any] = {}
        if mode is not None:
            update["mode"] = mode

        if model_name is not None:
            update["model_name"] = model_name

        return self.model_copy(update=update)


class RemoteProviderView(BaseModel):
    """Удалённый провайдер профиля чата глазами агента: вид и соединение."""

    model_config = ConfigDict(extra="ignore")

    kind: ProviderKind
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

    def model(self, name: str, profile: ChatProfileConfig) -> ModelSettings:
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

        if not view.kind.remote:
            msg = (
                f"chat profile {name!r}: provider kind {view.kind.value!r} runs in "
                "the process of the agent, expected a remote provider (openai or "
                "ollama) with an http connection"
            )
            raise AgentSettingsError(msg)

        base_url = str(view.connection.public_url())
        if not base_url:
            msg = f"chat profile {name!r}: the provider connection gives no address"
            raise AgentSettingsError(msg)

        api_key = None
        if isinstance(view.connection.auth, BearerAuth):
            api_key = view.connection.auth.token

        provider = ProviderSettings(kind=view.kind, base_url=base_url, api_key=api_key)

        return ModelSettings(
            provider=provider,
            model=profile.model,
            sampling=dict(profile.sampling),
            context_window=self._context_window,
            max_output_tokens=self._max_output_tokens,
        )


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
    Единственный, кто пишет слои в таблицу: копия при открытии и правка панели.

    Создаётся провайдером процесса; читает запуск агента (AgentLauncher),
    правку приносит AgentSettingsChanges.
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

    async def change(
        self,
        thread_id: UUID,
        user_id: UUID,
        name: str,
        profile: ChatProfileConfig,
        change: AgentSettingsChange,
    ) -> ChatAgentSettings:
        """Правка панели в оба слоя; итог — новый слой чата. Слой пользователя
        заменяется целиком; в слой чата поля провайдера ложатся, только если
        слой пользователя изменился, иначе чат остаётся при своих; выбор
        вкладки «Сессия» пишется только в слой чата.

        Ошибки:
        AgentSettingsError — провайдер профиля не годится агенту.
        """
        saved = await self._table.user_layer(user_id, name)
        chat = await self.chat_layer(thread_id, user_id, name, profile)
        if change.user != saved:
            await self._table.save_user_layer(user_id, name, change.user)
            based = self._specs.chat_settings(name, profile).with_user(change.user)
            chat = chat.model_copy(update={"model": based.model})

        chat = chat.with_session(change.mode, change.model_name)
        await self._table.save_chat_layer(thread_id, user_id, name, chat)

        return chat


class SettingsFileBuilder:
    """Слой файла настроек агента из слоя чата и конфига [agent]: история и
    режим разрешений добавляются к профилю модели чата; строка подключения
    истории — подключение процесса [postgres]."""

    def __init__(self, config: AgentConfig, postgres: PostgresConfig) -> None:
        self._config = config
        self._libpq = LibpqConnection(postgres)

    def build(self, chat: ChatAgentSettings) -> SettingsLayer:
        """Слой файла процесса агента по слою чата."""
        permissions = PermissionSettings(default_mode=chat.mode, allow_bypass=True)
        model = chat.model.revealed()

        return SettingsLayer(
            model=chat.model_name,
            models={chat.model_name: model},
            permissions=permissions,
            history=self._history(),
            system_prompt_file=WorkdirFile.SYSTEM_PROMPT.value,
        )

    def _history(self) -> PostgresHistorySettings | MemoryHistorySettings:
        history = self._config.history
        if isinstance(history, PostgresAgentHistory):
            return PostgresHistorySettings(
                kind=HistoryKind.POSTGRES,
                dsn=self._libpq.dsn(),
                schema=history.db_schema,
            )

        if isinstance(history, MemoryAgentHistory):
            return MemoryHistorySettings(kind=HistoryKind.MEMORY)

        msg = f"[agent.history] of kind {history.kind!r} has no file form"
        raise AgentSettingsError(msg)


class HistoryEnvironment:
    """Окружение libkrb5 для процесса агента с kerberos-ролью истории: путь к
    кэшу билета и к krb5.conf рабочего каталога процесса aid; билет в кэше
    обновляется по keytab до запуска. У парольной роли окружение пустое.

    Исключение §17: libpq читает KRB5CCNAME/KRB5_CONFIG из окружения сам;
    это пути к файлам того же пользователя, не секреты.
    """

    def __init__(self, history: AgentHistoryConfig, postgres: PostgresConfig) -> None:
        self._history = history
        self._libpq = LibpqConnection(postgres)

    async def prepare(self) -> dict[str, str]:
        """Ошибки:
        KerberosError — билет по keytab не получен.
        """
        if not isinstance(self._history, PostgresAgentHistory):
            return {}

        auth = self._libpq.kerberos_auth()
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

    def write(self, layer: SettingsLayer, system_prompt: str) -> None:
        """Файл настроек с открытыми секретами и промпт с правами 0600."""
        text = json.dumps(layer.revealed(), ensure_ascii=False, indent=2)
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
