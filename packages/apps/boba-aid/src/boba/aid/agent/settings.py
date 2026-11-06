"""Настройки агента: модель файла настроек глазами чата, его сборка из
профиля чата и конфига [agent] и приватный каталог процесса.

Файл настроек — договор запуска агента: чат пишет в него то, чему в ACP
нет пути, — провайдера и модель профиля, системный промпт, хранилище
истории, сроки серверов MCP, политику запуска `permissions.allowBypass` и
окружение libkrb5. Схема файла принадлежит агенту и публикуется им командой
`boba settings-schema`; модели этого модуля — собственная запись чата по тем
же ключам (camelCase), её сверяет со схемой контрактный тест. Режим
разрешений и профиль модели сессии чат выставляет по ACP (`configOptions`,
`session/set_config_option`), серверы MCP объявляет в методах сессии.

Файл собирается из текущего профиля чата и конфига [agent] при каждом
запуске процесса агента, в том числе короткого процесса `session/delete` при
удалении треда. Пользователь настроек агента не меняет. Секреты лежат в файле
открыто под правами 0600.

Ошибки:
AgentSettingsError — профиль чата нельзя превратить в профиль модели агента.
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
from typing import ClassVar

from pydantic import (
    ValidationError,
)

from boba.aid.config.models import (
    AgentHistoryConfig,
    AgentHistoryKind,
    AgentProviderKind,
    HistoryPoolFile,
    LimitsFile,
    McpFile,
    MemoryAgentHistory,
    MemoryHistoryFile,
    ModelFile,
    PermissionsFile,
    PostgresAgentHistory,
    PostgresHistoryFile,
    ProcessAgentConfig,
    ProviderFile,
    RemoteProviderView,
    SettingsFile,
    ToolsFile,
)
from boba.aid.mcp_servers.sources import ServerRecord
from boba.chat.profiles import (
    ChatProfileConfig,
    LocalChatProfileConfig,
)
from boba.db.postgres.connection import LibpqConnection
from boba.transport.http.connection import NegotiateAuth

__all__ = [
    "AgentProviderKind",
    "AgentSettingsError",
    "AgentWorkdir",
    "AgentWorkdirs",
    "HistoryEnvironment",
    "HistoryPoolFile",
    "McpFile",
    "MemoryHistoryFile",
    "ModelFile",
    "PermissionsFile",
    "PostgresHistoryFile",
    "ProfileModelSpecs",
    "ProviderFile",
    "SettingsFile",
    "SettingsFileBuilder",
    "ToolsFile",
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


class ProfileModelSpecs:
    """Профиль модели агента из профиля чата: адрес, вход, срок, TLS и
    повторы — из соединения провайдера, транспорт — из общей секции [http],
    окно, потолок ответа и возврат рассуждений — из пределов
    профиля ([model_limits.<имя>]).

    Создаёт его запуск агента (AgentLauncher)."""

    def model(self, name: str, profile: ChatProfileConfig) -> ModelFile:
        """Ошибки:
        AgentSettingsError — провайдер профиля не удалённый, без адреса или
            со входом negotiate, который в процесс агента не передать.
        """
        if not isinstance(profile, LocalChatProfileConfig):
            raise AgentSettingsError(
                f"chat profile {name!r} has no local model settings"
            )
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

        connection = view.connection
        if isinstance(connection.auth, NegotiateAuth):
            msg = (
                f"chat profile {name!r}: provider login 'negotiate' cannot be handed "
                "to the agent process, its kerberos credentials stay in the chat; "
                "expected method 'none', 'basic', 'bearer' or 'digest', or run the "
                "agent as a service with its own keytab"
            )
            raise AgentSettingsError(msg)

        provider = ProviderFile(
            kind=view.kind,
            base_url=base_url,
            auth=connection.auth,
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


class SettingsFileBuilder:
    """Файл настроек агента из профиля модели и конфига: к профилю модели чата
    добавляются политика запуска `allowBypass` (режим сессии чат выставляет
    по ACP), история, инструменты «всегда загружать» [agent.tools], пределы
    хода [agent.limits], срок вызова инструментов — наибольший
    `limits.call_timeout_sec` серверов, выданных треду, — и окружение libkrb5
    истории ключом `env`. Строка подключения истории — её собственное
    подключение [agent.history].postgres. Файл — единственный канал настроек
    запуска агента: argv называет только его, окружение ребёнка — минимальный
    набор SDK."""

    def __init__(self, config: ProcessAgentConfig) -> None:
        self._history = self._history_file(config.history)
        self._tools = ToolsFile(always_load=config.tools.always_load)
        self._limits = LimitsFile(
            tool_result_window_share=config.limits.tool_result_window_share
        )

    def build(
        self,
        name: str,
        model: ModelFile | None,
        mcp: McpFile | None,
        env: Mapping[str, str],
    ) -> SettingsFile:
        """Файл процесса агента с единственным профилем модели name; mcp None
        — процесс не зовёт инструментов серверов (короткий процесс удаления
        сессии)."""
        if model is None:
            raise AgentSettingsError("a local agent settings file requires a model")
        return SettingsFile(
            model=name,
            models={name: model},
            permissions=PermissionsFile(allow_bypass=True),
            history=self._history,
            mcp=mcp,
            tools=self._tools,
            limits=self._limits,
            env=env,
            system_prompt_file=WorkdirFile.SYSTEM_PROMPT.value,
        )

    @staticmethod
    def mcp(servers: Sequence[ServerRecord]) -> McpFile | None:
        """Сроки серверов MCP агента: вызов не обрывается раньше, чем его
        оборвал бы ретранслятор чата у самого терпеливого сервера набора;
        None — серверов у треда нет."""
        limits: list[float] = []
        for server in servers:
            limits.append(server.config.limits.call_timeout_sec)

        if not limits:
            return None

        return McpFile(call_timeout_sec=max(limits))

    @staticmethod
    def _history_file(
        history: AgentHistoryConfig,
    ) -> PostgresHistoryFile | MemoryHistoryFile:
        if isinstance(history, PostgresAgentHistory):
            pool = history.postgres.pool
            max_size = pool.min_size
            if pool.max_size is not None:
                max_size = pool.max_size

            return PostgresHistoryFile(
                kind=AgentHistoryKind.POSTGRES,
                dsn=LibpqConnection(history.postgres).dsn(),
                schema=history.db_schema,
                pool=HistoryPoolFile(
                    min_size=pool.min_size, max_size=max_size, timeout_sec=pool.timeout
                ),
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
