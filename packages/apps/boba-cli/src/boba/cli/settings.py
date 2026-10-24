"""Настройки процесса агента: слои `settings.json`, аргументы, окружение.

Устройство — план, раздел 5.21. Источники сливаются по приоритету снизу
вверх: значения по умолчанию, файл пользователя, файл проекта, локальный
файл проекта, файлы и строки `--settings`, аргументы запуска,
административный слой `--policy`. Поле побеждает полем, таблицы (`models`,
`env`) сливаются по именам, списки правил
объединяются. Итог — одна модель EffectiveSettings, из которой строятся
профиль модели, пределы, режим и сроки ожидания серверов MCP для ядра.
Серверы MCP приходят только по протоколу (`session/new`).
Секреты — ключ провайдера `apiKey` и строка подключения `dsn` — лежат
значениями в файле настроек; такой файл принимается только с правами
не шире 0600, а в строке аргумента секреты запрещены. Чтение секрета по имени
переменной (`apiKeyEnv`, `dsnEnv`) остаётся для ручного запуска; окружение
процесса читается только здесь, минимальный профиль без файлов —
из `BOBA_LLM_*`. Режим разрешений и право на обход в аргументах не
передаются: это ключ `permissions` файла.

Ошибки:
SettingsError — файл не прочитан или не разобран, слой не проходит модель,
    файл с секретами доступен не только владельцу, действующих настроек не
    хватает для запуска: нет профиля модели, нет переменной с ключом.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeGuard

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
)

from boba.agent.profile import (
    CompactionSettings,
    ModelProfile,
    TurnLimits,
)
from boba.agent.records import PermissionMode
from boba.agent.settings import (
    HistoryKind,
    JsonlHistorySettings,
    LimitsJson,
    McpClientJson,
    MemoryHistorySettings,
    ModelSettings,
    PermissionSettings,
    PostgresHistorySettings,
    ProviderKind,
    ProviderSettings,
    SecretKey,
    SessionsJson,
    SettingsLayer,
    ToolsJson,
)
from boba.db.postgres.connection import PostgresConfig
from boba.llm.providers import ChatModelConfig, LlmProvidersError

__all__ = [
    "Arguments",
    "EffectiveSettings",
    "EnvName",
    "PostgresBuilder",
    "SecretsGuard",
    "Settings",
    "SettingsError",
    "SettingsSource",
]


class SettingsError(Exception):
    """Настройки не прочитаны, не разобраны или их не хватает для запуска."""


class SettingsFile(StrEnum):
    """Файлы настроек относительно дома и каталога проекта."""

    USER = ".boba/settings.json"
    PROJECT = ".boba/settings.json"
    LOCAL = ".boba/settings.local.json"
    ENCODING = "utf-8"


class SettingsSource(StrEnum):
    """Источники файловых слоёв, которые можно включить `--setting-sources`."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


class EnvName(StrEnum):
    """Переменные окружения, которые читает граница настроек."""

    HISTORY_ROOT = "BOBA_HISTORY_ROOT"
    LLM_KIND = "BOBA_LLM_KIND"
    LLM_BASE_URL = "BOBA_LLM_BASE_URL"
    LLM_MODEL = "BOBA_LLM_MODEL"
    LLM_API_KEY = "BOBA_LLM_API_KEY"
    SHELL = "SHELL"


WebScheme = Literal["http", "https"]


@dataclass(frozen=True)
class WebAddress:
    """Разобранный абсолютный http(s) адрес из настроек."""

    scheme: WebScheme
    host: str
    port: int | None
    path: str


class HttpUrls:
    """Разбор адреса из настроек: принимается только абсолютный http(s) URL."""

    SCHEMES: ClassVar[frozenset[WebScheme]] = frozenset({"http", "https"})

    def absolute(self, value: str, what: str) -> WebAddress:
        """Ошибки:
        ValueError — строка не URL, схема не http(s) или нет хоста.
        """
        try:
            url = httpx.URL(value)
        except (httpx.InvalidURL, TypeError) as exc:
            msg = f"{what} '{value}' is not a valid URL: {exc}"
            raise ValueError(msg) from exc

        scheme = url.scheme
        if scheme not in self.SCHEMES:
            msg = f"{what} '{value}' must be an absolute http or https URL."
            raise ValueError(msg)

        if not url.host:
            msg = f"{what} '{value}' names no host; expected an absolute URL."
            raise ValueError(msg)

        return WebAddress(scheme=scheme, host=url.host, port=url.port, path=url.path)


class SecretsGuard:
    """Правила чтения слоя с секретами: только из файла, доступного одному
    владельцу; строка аргумента секретов не несёт."""

    GROUP_AND_OTHERS: ClassVar[int] = 0o077
    OWNER_ONLY: ClassVar[int] = 0o600

    def file(self, layer: SettingsLayer, path: Path, what: str) -> None:
        """Ошибки:
        SettingsError — файл с секретами читается не только владельцем или
            его права не узнать.
        """
        found = list(layer.secrets())
        if not found:
            return

        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            msg = f"Cannot check the permissions of {what} at {path}: {exc}"
            raise SettingsError(msg) from exc

        if mode & self.GROUP_AND_OTHERS:
            msg = (
                f"{what} at {path} holds secrets ({', '.join(found)}) but has mode "
                f"{mode:04o}, readable beyond its owner. A file with secrets must "
                f"have mode {self.OWNER_ONLY:04o}: run "
                f"chmod {self.OWNER_ONLY:o} {path}."
            )
            raise SettingsError(msg)

    def inline(self, layer: SettingsLayer, what: str) -> None:
        """Ошибки:
        SettingsError — секрет пришёл строкой аргумента.
        """
        found = list(layer.secrets())
        if not found:
            return

        msg = (
            f"{what} passes secrets ({', '.join(found)}) on the command line, where "
            "other processes can read them. Put the layer in a file with mode "
            f"{self.OWNER_ONLY:04o} and pass its path."
        )
        raise SettingsError(msg)


@dataclass(frozen=True)
class Arguments:
    """Параметры запуска после разбора командной строки (план, раздел 5.21):
    то, что меняется от запуска к запуску и секретов не несёт."""

    model: str | None = None
    system_prompt: str | None = None
    system_prompt_file: str | None = None
    append_system_prompt: str | None = None
    append_system_prompt_file: str | None = None
    skills_dir: str | None = None
    max_turns: int | None = None
    settings: Sequence[str] = ()
    setting_sources: Sequence[SettingsSource] = tuple(SettingsSource)
    policy: str | None = None
    history_root: str | None = None
    record_requests: bool = False

    def layer(self) -> SettingsLayer:
        """Аргументы как слой настроек: то, что в них перекрывает файлы."""
        history: JsonlHistorySettings | None = None
        if self.history_root is not None:
            history = JsonlHistorySettings(
                kind=HistoryKind.JSONL, root=self.history_root
            )

        limits: LimitsJson | None = None
        if self.max_turns is not None:
            limits = LimitsJson(max_turns=self.max_turns)

        return SettingsLayer(
            model=self.model,
            history=history,
            limits=limits,
            system_prompt_file=self.system_prompt_file,
            skills_dir=self.skills_dir,
        )


class LayerMerge:
    """Слияние слоёв: поле побеждает полем, таблицы — по ключам, списки
    объединяются без повторов. Секция с дискриминатором `kind` (история)
    при смене вида заменяется целиком: поля разных видов не смешиваются."""

    DISCRIMINATORS: ClassVar[tuple[str, ...]] = ("kind",)

    def merged(self, lower: SettingsLayer, upper: SettingsLayer) -> SettingsLayer:
        # режим python: секреты остаются SecretStr, а не маской из режима json
        low = lower.model_dump(exclude_none=True, by_alias=True, mode="python")
        up = upper.model_dump(exclude_none=True, by_alias=True, mode="python")

        return SettingsLayer.model_validate(self._merge(low, up))

    def _merge(self, low: Mapping[str, Any], up: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(low)
        for key, value in up.items():
            below = result.get(key)
            if isinstance(below, Mapping) and isinstance(value, Mapping):
                if self._kind_changed(below, value):
                    result[key] = dict(value)
                    continue

                result[key] = self._merge(below, value)
                continue

            if self._is_list(below) and self._is_list(value):
                result[key] = self._union(below, value)
                continue

            result[key] = value

        return result

    def _kind_changed(self, below: Mapping[str, Any], above: Mapping[str, Any]) -> bool:
        for key in self.DISCRIMINATORS:
            if self._differs(below.get(key), above.get(key)):
                return True

        return False

    @staticmethod
    def _differs(kind_below: Any, kind_above: Any) -> bool:
        if kind_below is None:
            return False

        if kind_above is None:
            return False

        return kind_below != kind_above

    @staticmethod
    def _is_list(value: Any) -> TypeGuard[Sequence[Any]]:
        """Список слоя: в режиме python последовательности приходят и списками,
        и кортежами."""
        return isinstance(value, (list, tuple))

    @staticmethod
    def _union(below: Sequence[Any], above: Sequence[Any]) -> list[Any]:
        joined: list[Any] = list(below)
        for item in above:
            if item not in joined:
                joined.append(item)

        return joined


class JsonText:
    """Чтение JSON из файла или из строки аргумента: строка, начинающаяся
    с `{`, — сам документ, иначе — путь к файлу."""

    @staticmethod
    def is_inline(source: str) -> bool:
        return source.lstrip().startswith("{")

    def read_file(self, path: Path, what: str) -> str:
        try:
            return path.read_text(encoding=SettingsFile.ENCODING.value)
        except OSError as exc:
            msg = f"Cannot read {what} from {path}: {exc}"
            raise SettingsError(msg) from exc

    @staticmethod
    def parse(text: str, what: str, source: str) -> Mapping[str, Any]:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            msg = f"{what} at {source} is not valid JSON: {exc}"
            raise SettingsError(msg) from exc

        if not isinstance(document, Mapping):
            kind = type(document).__name__
            msg = f"{what} at {source} must be a JSON object, got {kind}"
            raise SettingsError(msg)

        return document


class EffectiveSettings(BaseModel):
    """Действующие настройки после слияния: то, из чего собирается ядро."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    DEFAULT_AGENT_NAME: ClassVar[str] = "Boba"

    model: str
    models: Mapping[str, ModelSettings]
    permissions: PermissionSettings
    history: JsonlHistorySettings | PostgresHistorySettings | MemoryHistorySettings
    compaction: CompactionSettings
    limits: TurnLimits
    mcp: McpClientJson
    tools: ToolsJson
    sessions: SessionsJson
    env: Mapping[str, str]
    system_prompt: Sequence[str]
    """Секции промпта настроек: промпт аргумента или файла и дополнения."""
    appended_prompt: Sequence[str]
    """Дополнения промпта из аргументов: они идут и за промптом профиля."""
    argument_prompt: bool
    """Промпт задан аргументом --system-prompt: он сильнее промпта профиля."""
    skills_dir: str | None
    agent_name: str
    record_requests: bool

    @property
    def permission_mode(self) -> PermissionMode:
        mode = self.permissions.default_mode
        if mode is None:
            return PermissionMode.DEFAULT

        return mode

    @property
    def allow_bypass(self) -> bool:
        return bool(self.permissions.allow_bypass)

    def selected_model(self) -> ModelSettings:
        found = self.models.get(self.model)
        if found is None:
            known = sorted(self.models)
            msg = (
                f"The model profile '{self.model}' is not defined. Known profiles: "
                f"{known}. Add it under 'models' in settings.json or pick another "
                "with --model."
            )
            raise SettingsError(msg)

        return found

    def system_prompt_of(self, name: str) -> Sequence[str]:
        """Секции системного промпта сессии на профиле name: свой промпт
        профиля с дополнениями, иначе промпт настроек; промпт аргумента
        сильнее промпта профиля."""
        own = self.models[name].system_prompt
        if own is None:
            return self.system_prompt

        if self.argument_prompt:
            return self.system_prompt

        return [own, *self.appended_prompt]

    def public(self) -> dict[str, Any]:
        """Настройки для события `init`: без значений окружения;
        секреты выходят маской SecretStr."""
        document = self.model_dump(mode="json", by_alias=True)
        document["env"] = sorted(self.env)

        return document


class ProfileBuilder:
    """ModelProfile из настроек профиля: ключ провайдера — значением из файла
    или из окружения по имени переменной, адрес — частями через httpx.URL,
    срок, TLS, повторы и транспорт — как в файле; таблицы проверяет модель
    провайдера."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self._urls = HttpUrls()

    def profile(self, name: str, settings: ModelSettings) -> ModelProfile:
        provider = self._provider(name, settings.provider)
        try:
            chat = ChatModelConfig.model_validate(
                {
                    "provider": provider,
                    "model": settings.model,
                    "sampling": dict(settings.sampling),
                }
            )
        except (ValidationError, LlmProvidersError) as exc:
            msg = (
                f"The model profile '{name}' does not describe a usable provider "
                f"of kind '{settings.provider.kind.value}': {exc}"
            )
            raise SettingsError(msg) from exc

        return ModelProfile(
            chat=chat,
            context_window=settings.context_window,
            max_output_tokens=settings.max_output_tokens,
            chars_per_token=settings.chars_per_token,
            supports_tools=settings.supports_tools,
            supports_images=settings.supports_images,
            system_turns=settings.system_turns,
            reasoning_return=settings.reasoning_return,
        )

    def _provider(self, name: str, provider: ProviderSettings) -> dict[str, Any]:
        if not provider.kind.remote:
            if provider.model_dir is None:
                msg = (
                    f"The model profile '{name}' has provider kind 'onnx' but no "
                    "'modelDir' with the weights directory."
                )
                raise SettingsError(msg)

            return {"kind": provider.kind.value, "model_dir": provider.model_dir}

        if provider.base_url is None:
            msg = (
                f"The model profile '{name}' has provider kind "
                f"'{provider.kind.value}' but no 'baseUrl' of the API."
            )
            raise SettingsError(msg)

        return {
            "kind": provider.kind.value,
            "connection": self._connection(name, provider),
            "transport": dict(provider.transport),
        }

    def _connection(self, name: str, provider: ProviderSettings) -> dict[str, Any]:
        url = self._url(name, provider)
        auth: dict[str, Any] = {"method": "none"}
        if token := self._token(name, provider):
            auth = {"method": "bearer", "token": token}

        connection: dict[str, Any] = {
            "scheme": url.scheme,
            "host": url.host,
            "path": url.path,
            "auth": auth,
            "timeout_sec": provider.timeout_sec,
            "ssl_verify": provider.ssl_verify,
            "retry_attempts": provider.retry_attempts,
            "retry_backoff_sec": provider.retry_backoff_sec,
            "retry_statuses": dict(provider.retry_statuses),
            "retry_after_max_sec": provider.retry_after_max_sec,
        }
        if url.port is not None:
            connection["port"] = url.port

        return connection

    def _token(self, name: str, provider: ProviderSettings) -> SecretStr | None:
        """Ключ провайдера: значение из файла, иначе из названной переменной;
        профиль без того и другого ходит без ключа."""
        if provider.api_key is not None:
            return provider.api_key

        if provider.api_key_env is None:
            return None

        token = self._env.get(provider.api_key_env)
        if not token:
            msg = (
                f"The model profile '{name}' takes its API key from the "
                f"environment variable {provider.api_key_env}, but it is not set. "
                "Export it before starting the agent."
            )
            raise SettingsError(msg)

        return SecretStr(token)

    def _url(self, name: str, provider: ProviderSettings) -> WebAddress:
        base_url = provider.base_url
        if base_url is None:
            base_url = ""

        try:
            return self._urls.absolute(base_url, f"the model profile '{name}' baseUrl")
        except ValueError as exc:
            raise SettingsError(str(exc)) from exc


class PostgresBuilder:
    """PostgresConfig из строки подключения libpq — значения `dsn` из файла
    настроек или переменной окружения, названной `dsnEnv`; пароль живёт
    в SecretStr и в публичные настройки не попадает. Имя агента в
    pg_stat_activity — запасное: application_name строки подключения главнее."""

    APPLICATION: ClassVar[str] = "boba"

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def config(self, settings: PostgresHistorySettings) -> PostgresConfig:
        origin = self._origin(settings)
        dsn = self._dsn(settings, origin)
        try:
            parts = conninfo_to_dict(dsn)
        except psycopg.ProgrammingError as exc:
            msg = f"{origin} is not a valid libpq connection string: {exc}"
            raise SettingsError(msg) from exc

        user = parts.pop("user", None)
        password = parts.pop("password", None)
        if not user:
            msg = (
                f"{origin} names no user; the agent connects to Postgres as "
                "a role with a password or trusted by pg_hba."
            )
            raise SettingsError(msg)

        auth: dict[str, Any] = {"method": "trust", "user": str(user)}
        if password:
            auth = {
                "method": "password",
                "user": str(user),
                "password": SecretStr(str(password)),
            }

        pool = settings.pool
        document: dict[str, Any] = {
            **parts,
            "fallback_application_name": self.APPLICATION,
            "auth": auth,
            "pool": {
                "min_size": pool.min_size,
                "max_size": pool.max_size,
                "timeout": pool.timeout_sec,
            },
        }
        try:
            return PostgresConfig.model_validate(document)
        except ValidationError as exc:
            msg = f"{origin} does not describe a usable connection: {exc}"
            raise SettingsError(msg) from exc

    @staticmethod
    def _origin(settings: PostgresHistorySettings) -> str:
        """Откуда взята строка подключения — для текста ошибок."""
        if settings.dsn is not None:
            return f"history.{SecretKey.DSN.value} in the settings file"

        return f"the environment variable {settings.dsn_env}"

    def _dsn(self, settings: PostgresHistorySettings, origin: str) -> str:
        if settings.dsn is not None:
            return settings.dsn.get_secret_value()

        if settings.dsn_env is None:
            msg = "history of kind 'postgres' has neither 'dsn' nor 'dsnEnv'."
            raise SettingsError(msg)

        dsn = self._env.get(settings.dsn_env)
        if not dsn:
            msg = (
                f"The history store takes its Postgres connection string from "
                f"{origin}, but it is not set."
            )
            raise SettingsError(msg)

        return dsn


class Settings:
    """Чтение слоёв и слияние в действующие настройки одного запуска."""

    DEFAULT_HISTORY_DIR: ClassVar[str] = ".boba/history"
    DEFAULT_PROFILE: ClassVar[str] = "default"
    DEFAULT_WINDOW: ClassVar[int] = 32_000
    DEFAULT_OUTPUT: ClassVar[int] = 4_096

    def __init__(
        self, arguments: Arguments, env: Mapping[str, str], home: Path, cwd: Path
    ) -> None:
        self._arguments = arguments
        self._env = env
        self._home = home
        self._cwd = cwd
        self._json = JsonText()
        self._merge = LayerMerge()
        self._guard = SecretsGuard()

    def effective(self) -> EffectiveSettings:
        """Слияние всех слоёв по приоритету."""
        layer = self._defaults()
        for upper in self._layers():
            layer = self._merge.merged(layer, upper)

        return self._effective(layer)

    def effective_of(self, layer: SettingsLayer) -> EffectiveSettings:
        """Действующие настройки одного готового слоя поверх нижнего слоя по
        умолчанию, без файлов и аргументов: так их собирает `boba serve` из
        своего конфига."""
        return self._effective(self._merge.merged(self._defaults(), layer))

    def layer_of(self, document: Mapping[str, Any], what: str) -> SettingsLayer:
        try:
            return SettingsLayer.model_validate(document)
        except ValidationError as exc:
            msg = f"{what} does not match the settings schema: {exc}"
            raise SettingsError(msg) from exc

    def _layers(self) -> Iterator[SettingsLayer]:
        sources = set(self._arguments.setting_sources)
        if SettingsSource.USER in sources:
            yield from self._file_layer(self._home / SettingsFile.USER.value, "user")

        if SettingsSource.PROJECT in sources:
            yield from self._file_layer(
                self._cwd / SettingsFile.PROJECT.value, "project"
            )

        if SettingsSource.LOCAL in sources:
            yield from self._file_layer(self._cwd / SettingsFile.LOCAL.value, "local")

        for source in self._arguments.settings:
            yield self._argument_layer(source)

        yield self._arguments.layer()
        if self._arguments.policy is not None:
            yield self._named_file_layer(Path(self._arguments.policy), "--policy")

    def _argument_layer(self, source: str) -> SettingsLayer:
        """Слой `--settings`: путь к файлу, проверяемому на права, либо
        строка JSON, в которой секретов быть не должно."""
        if self._json.is_inline(source):
            document = self._json.parse(source, "--settings", source)
            layer = self.layer_of(document, f"--settings {source}")
            self._guard.inline(layer, "--settings")

            return layer

        return self._named_file_layer(Path(source), "--settings")

    def _named_file_layer(self, path: Path, what: str) -> SettingsLayer:
        text = self._json.read_file(path, what)
        document = self._json.parse(text, what, str(path))
        layer = self.layer_of(document, f"{what} {path}")
        self._guard.file(layer, path, what)

        return layer

    def _file_layer(self, path: Path, what: str) -> Iterator[SettingsLayer]:
        if not path.is_file():
            return

        text = self._json.read_file(path, f"{what} settings")
        document = self._json.parse(text, f"{what} settings", str(path))
        layer = self.layer_of(document, f"{what} settings at {path}")
        self._guard.file(layer, path, f"{what} settings")

        yield layer

    def _defaults(self) -> SettingsLayer:
        """Нижний слой: журнал в доме, профиль из `BOBA_LLM_*`, если задан."""
        root = self._env.get(EnvName.HISTORY_ROOT.value)
        if not root:
            root = str(self._home / self.DEFAULT_HISTORY_DIR)

        layer = SettingsLayer(
            history=JsonlHistorySettings(kind=HistoryKind.JSONL, root=root)
        )
        profile = self._env_profile()
        if profile is None:
            return layer

        return layer.model_copy(
            update={
                "model": self.DEFAULT_PROFILE,
                "models": {
                    self.DEFAULT_PROFILE: profile.model_dump(by_alias=True, mode="json")
                },
            }
        )

    def _env_profile(self) -> ModelSettings | None:
        kind = self._env.get(EnvName.LLM_KIND.value)
        base_url = self._env.get(EnvName.LLM_BASE_URL.value)
        model = self._env.get(EnvName.LLM_MODEL.value)
        if not kind or not base_url or not model:
            return None

        api_key_env: str | None = None
        if self._env.get(EnvName.LLM_API_KEY.value):
            api_key_env = EnvName.LLM_API_KEY.value

        try:
            provider = ProviderSettings(
                kind=ProviderKind(kind), base_url=base_url, api_key_env=api_key_env
            )
        except ValueError as exc:
            msg = (
                f"{EnvName.LLM_KIND.value}={kind!r} is not a provider kind; expected "
                f"one of {[k.value for k in ProviderKind]}."
            )
            raise SettingsError(msg) from exc

        return ModelSettings(
            provider=provider,
            model=model,
            context_window=self.DEFAULT_WINDOW,
            max_output_tokens=self.DEFAULT_OUTPUT,
        )

    def _effective(self, layer: SettingsLayer) -> EffectiveSettings:
        if layer.model is None or not layer.models:
            msg = (
                "No model profile is configured. Add 'models' with a default 'model' "
                "to settings.json (user, project or --settings), or set "
                f"{EnvName.LLM_KIND.value}, {EnvName.LLM_BASE_URL.value}, "
                f"{EnvName.LLM_MODEL.value} and {EnvName.LLM_API_KEY.value}."
            )
            raise SettingsError(msg)

        if layer.history is None:
            msg = "No history store is configured. This is a bug: defaults set one."
            raise SettingsError(msg)

        permissions = layer.permissions
        if permissions is None:
            permissions = PermissionSettings()

        compaction = CompactionSettings()
        if layer.compaction is not None:
            compaction = layer.compaction.core()

        limits = self._limits(layer)

        mcp = layer.mcp
        if mcp is None:
            mcp = McpClientJson()

        tools = layer.tools
        if tools is None:
            tools = ToolsJson(always_load=())

        sessions = layer.sessions
        if sessions is None:
            sessions = SessionsJson()

        agent_name = layer.agent_name
        if agent_name is None:
            agent_name = EffectiveSettings.DEFAULT_AGENT_NAME

        return EffectiveSettings(
            model=layer.model,
            models=self._models(layer),
            permissions=permissions,
            history=layer.history,
            compaction=compaction,
            limits=limits,
            mcp=mcp,
            tools=tools,
            sessions=sessions,
            env=layer.env,
            system_prompt=list(self._system_prompt(layer)),
            appended_prompt=list(self._appended_prompt()),
            argument_prompt=self._arguments.system_prompt is not None,
            skills_dir=layer.skills_dir,
            agent_name=agent_name,
            record_requests=self._arguments.record_requests,
        )

    @staticmethod
    def _models(layer: SettingsLayer) -> dict[str, ModelSettings]:
        """Профили после слияния слоёв — полные модели."""
        models: dict[str, ModelSettings] = {}
        for name, raw in layer.models.items():
            try:
                models[name] = ModelSettings.model_validate(raw)
            except ValidationError as exc:
                msg = f"The model profile '{name}' is incomplete or invalid: {exc}"
                raise SettingsError(msg) from exc

        return models

    @staticmethod
    def _limits(layer: SettingsLayer) -> TurnLimits:
        """Пределы хода слоя; доля окна под результат инструмента задаётся
        только явно.

        Ошибки:
        SettingsError — limits.toolResultWindowShare не задан ни одним слоем.
        """
        missing = (
            "Settings have no limits.toolResultWindowShare: the share of the "
            "effective context window one tool result may take (0.25 in the "
            "shipped configs). Set it in settings.json under limits."
        )
        limits = layer.limits
        if limits is None:
            raise SettingsError(missing)

        if limits.tool_result_window_share is None:
            raise SettingsError(missing)

        return limits.core()

    def _system_prompt(self, layer: SettingsLayer) -> Iterator[str]:
        """Секции системного промпта: промпт аргумента или файла, затем
        дополнения; без них промпт пустой."""
        arguments = self._arguments
        if arguments.system_prompt is not None:
            yield arguments.system_prompt
        elif layer.system_prompt_file is not None:
            yield self._json.read_file(
                self._cwd / layer.system_prompt_file, "the system prompt"
            ).strip()

        yield from self._appended_prompt()

    def _appended_prompt(self) -> Iterator[str]:
        """Дополнения промпта из аргументов запуска."""
        arguments = self._arguments
        if arguments.append_system_prompt is not None:
            yield arguments.append_system_prompt

        if arguments.append_system_prompt_file is not None:
            yield self._json.read_file(
                self._cwd / arguments.append_system_prompt_file,
                "the appended system prompt",
            ).strip()
