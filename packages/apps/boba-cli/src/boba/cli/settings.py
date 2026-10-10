"""Настройки процесса агента: слои `settings.json`, `.mcp.json`, аргументы, окружение.

Устройство — план, раздел 5.21. Источники сливаются по приоритету снизу
вверх: значения по умолчанию, файл пользователя, файл проекта, локальный
файл проекта, файлы и строки `--settings`, аргументы запуска,
административный слой `--policy`. Системный промпт может прийти и командой
`initialize` после старта — это дело протокола, не настроек.
Поле побеждает полем, таблицы (`models`, `env`, серверы MCP) сливаются по
именам, списки правил объединяются. Итог — одна модель EffectiveSettings,
из которой строятся профиль модели, пределы, режим и серверы MCP для ядра.
Окружение процесса читается только здесь: ключ провайдера берётся по имени
переменной из профиля, минимальный профиль без файлов — из `BOBA_LLM_*`.

Ошибки:
SettingsError — файл не прочитан или не разобран, слой не проходит модель,
    действующих настроек не хватает для запуска: нет профиля модели, нет
    переменной с ключом, сервер MCP неподдерживаемого вида.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic.alias_generators import to_camel

from boba.agent.profile import (
    CompactionSettings,
    ModelProfile,
    ReasoningReturn,
    TurnLimits,
)
from boba.agent.records import PermissionMode
from boba.db.postgres.connection import PostgresConfig
from boba.llm.providers import ChatModelConfig, LlmProvidersError
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    McpServerConfig,
    NoAuth,
    StdioCommand,
)

__all__ = [
    "Arguments",
    "EffectiveSettings",
    "EnvName",
    "HistoryKind",
    "JsonlHistorySettings",
    "McpServerJson",
    "ModelSettings",
    "PermissionSettings",
    "PostgresBuilder",
    "PostgresHistorySettings",
    "ProviderKind",
    "ProviderSettings",
    "Settings",
    "SettingsError",
    "SettingsLayer",
    "SettingsSource",
]


class SettingsError(Exception):
    """Настройки не прочитаны, не разобраны или их не хватает для запуска."""


class SettingsFile(StrEnum):
    """Файлы настроек относительно дома и каталога проекта."""

    USER = ".boba/settings.json"
    PROJECT = ".boba/settings.json"
    LOCAL = ".boba/settings.local.json"
    MCP = ".mcp.json"
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


class ProviderKind(StrEnum):
    """Виды провайдеров модели, которые умеет собрать вход процесса."""

    OPENAI = "openai"
    OLLAMA = "ollama"
    ONNX = "onnx"

    @property
    def remote(self) -> bool:
        return self is not ProviderKind.ONNX


class HistoryKind(StrEnum):
    JSONL = "jsonl"
    POSTGRES = "postgres"


class McpTransportJson(StrEnum):
    """Виды серверов в `.mcp.json` формата Claude Code."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class SettingsModel(BaseModel):
    """Основа моделей настроек: ключи файлов — camelCase, лишние ключи — ошибка."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )


class ProviderSettings(SettingsModel):
    """Провайдер модели: вид, адрес и имя переменной с ключом либо каталог весов."""

    kind: ProviderKind
    base_url: str | None = None
    api_key_env: str | None = None
    model_dir: str | None = None
    timeout_sec: float = Field(default=120.0, gt=0)
    retry_attempts: int = Field(default=1, ge=0)


class ModelSettings(SettingsModel):
    """Ключ `models.<имя>`: всё, что ядро знает о модели (ModelProfile)."""

    provider: ProviderSettings
    model: str = Field(min_length=1)
    sampling: dict[str, Any] = Field(default_factory=dict)
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    chars_per_token: int = Field(default=3, gt=0)
    supports_tools: bool = True
    supports_images: bool = False
    system_turns: bool = False
    reasoning_return: ReasoningReturn = ReasoningReturn.NEVER


class PermissionSettings(SettingsModel):
    """Ключ `permissions`: режим по умолчанию, право на обход, правила."""

    default_mode: PermissionMode | None = None
    allow_bypass: bool | None = None
    allow: Sequence[str] = ()
    deny: Sequence[str] = ()


class JsonlHistorySettings(SettingsModel):
    kind: Literal[HistoryKind.JSONL]
    root: str = Field(min_length=1)


class PostgresHistorySettings(SettingsModel):
    """Журнал в Postgres: строка подключения libpq из переменной окружения,
    таблица — в названной схеме."""

    DEFAULT_SCHEMA: ClassVar[str] = "agent_history"

    kind: Literal[HistoryKind.POSTGRES]
    dsn_env: str = Field(min_length=1)
    db_schema: str = Field(default=DEFAULT_SCHEMA, min_length=1)


HistorySettings = Annotated[
    JsonlHistorySettings | PostgresHistorySettings, Field(discriminator="kind")
]


class CompactionJson(SettingsModel):
    compact_at: float | None = Field(default=None, gt=0, lt=1)
    block_at: float | None = Field(default=None, gt=0, le=1)

    def core(self) -> CompactionSettings:
        values = self.model_dump(exclude_none=True, by_alias=False)

        return CompactionSettings.model_validate(values)


class LimitsJson(SettingsModel):
    max_turns: int | None = Field(default=None, gt=0)
    tool_result_max_chars: int | None = Field(default=None, gt=0)
    api_retries: int | None = Field(default=None, ge=0)
    output_limit_retries: int | None = Field(default=None, ge=0)

    def core(self) -> TurnLimits:
        values = self.model_dump(exclude_none=True, by_alias=False)

        return TurnLimits.model_validate(values)


class McpClientJson(SettingsModel):
    """Ключ `mcp`: сроки клиента и выбор серверов из `.mcp.json`."""

    connect_timeout_sec: float = Field(default=30.0, gt=0)
    call_timeout_sec: float = Field(default=600.0, gt=0)
    enabled_servers: Sequence[str] | None = None
    disabled_servers: Sequence[str] = ()


class McpServerJson(SettingsModel):
    """Сервер в `.mcp.json` формата Claude Code; ключи файла — как там."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: McpTransportJson = McpTransportJson.STDIO
    command: str | None = None
    args: Sequence[str] = ()
    env: Mapping[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = Field(default_factory=dict)


class McpJson(SettingsModel):
    """Файл `.mcp.json`: таблица серверов по именам."""

    mcp_servers: Mapping[str, McpServerJson] = Field(default_factory=dict)


class SettingsLayer(SettingsModel):
    """Один слой `settings.json`; каждый ключ необязателен. Профили `models`
    лежат сырыми таблицами: слой вправе дополнить профиль одним полем, а
    целиком профиль проверяется после слияния."""

    model: str | None = None
    models: Mapping[str, Mapping[str, Any]] = Field(default_factory=dict)
    permissions: PermissionSettings | None = None
    history: HistorySettings | None = None
    compaction: CompactionJson | None = None
    limits: LimitsJson | None = None
    mcp: McpClientJson | None = None
    env: Mapping[str, str] = Field(default_factory=dict)
    system_prompt_file: str | None = None
    skills_dir: str | None = None
    agent_name: str | None = None


@dataclass(frozen=True)
class Arguments:
    """Параметры запуска после разбора командной строки (план, раздел 5.21)."""

    session: str | None = None
    resume: bool = False
    model: str | None = None
    permission_mode: PermissionMode | None = None
    allow_bypass: bool = False
    system_prompt: str | None = None
    system_prompt_file: str | None = None
    append_system_prompt: str | None = None
    append_system_prompt_file: str | None = None
    skills_dir: str | None = None
    max_turns: int | None = None
    mcp_config: Sequence[str] = ()
    strict_mcp_config: bool = False
    settings: Sequence[str] = ()
    setting_sources: Sequence[SettingsSource] = tuple(SettingsSource)
    policy: str | None = None
    history_root: str | None = None
    record_requests: bool = False
    env: Mapping[str, str] = field(default_factory=dict)

    def layer(self) -> SettingsLayer:
        """Аргументы как слой настроек: то, что в них перекрывает файлы."""
        permissions: PermissionSettings | None = None
        if self.permission_mode is not None or self.allow_bypass:
            allow_bypass: bool | None = None
            if self.allow_bypass:
                allow_bypass = True

            permissions = PermissionSettings(
                default_mode=self.permission_mode, allow_bypass=allow_bypass
            )

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
            permissions=permissions,
            history=history,
            limits=limits,
            env=dict(self.env),
            system_prompt_file=self.system_prompt_file,
            skills_dir=self.skills_dir,
        )


class LayerMerge:
    """Слияние слоёв: поле побеждает полем, таблицы — по ключам, списки
    объединяются без повторов."""

    def merged(self, lower: SettingsLayer, upper: SettingsLayer) -> SettingsLayer:
        low = lower.model_dump(exclude_none=True, by_alias=True, mode="json")
        up = upper.model_dump(exclude_none=True, by_alias=True, mode="json")

        return SettingsLayer.model_validate(self._merge(low, up))

    def _merge(self, low: Mapping[str, Any], up: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(low)
        for key, value in up.items():
            below = result.get(key)
            if isinstance(below, Mapping) and isinstance(value, Mapping):
                result[key] = self._merge(below, value)
                continue

            if isinstance(below, list) and isinstance(value, list):
                result[key] = self._union(below, value)
                continue

            result[key] = value

        return result

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

    def read(self, source: str, what: str) -> Mapping[str, Any]:
        text = source
        if not source.lstrip().startswith("{"):
            text = self.read_file(Path(source), what)

        return self.parse(text, what, source)

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
    history: JsonlHistorySettings | PostgresHistorySettings
    compaction: CompactionSettings
    limits: TurnLimits
    mcp: McpClientJson
    mcp_servers: Mapping[str, McpServerJson]
    env: Mapping[str, str]
    system_prompt: Sequence[str]
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

    def public(self) -> dict[str, Any]:
        """Настройки для события `init`: без значений окружения серверов."""
        document = self.model_dump(mode="json", by_alias=True)
        document["env"] = sorted(self.env)

        return document


class ProfileBuilder:
    """ModelProfile из настроек профиля: ключ провайдера — из окружения по
    имени переменной, адрес — частями через httpx.URL."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

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
            "transport": {},
        }

    def _connection(self, name: str, provider: ProviderSettings) -> dict[str, Any]:
        url = self._url(name, provider)
        auth: dict[str, Any] = {"method": "none"}
        if provider.api_key_env is not None:
            token = self._env.get(provider.api_key_env)
            if not token:
                msg = (
                    f"The model profile '{name}' takes its API key from the "
                    f"environment variable {provider.api_key_env}, but it is not set. "
                    "Export it before starting the agent."
                )
                raise SettingsError(msg)

            auth = {"method": "bearer", "token": SecretStr(token)}

        connection: dict[str, Any] = {
            "scheme": url.scheme,
            "host": url.host,
            "path": url.path,
            "auth": auth,
            "timeout_sec": provider.timeout_sec,
            "retry_attempts": provider.retry_attempts,
        }
        if url.port is not None:
            connection["port"] = url.port

        return connection

    @staticmethod
    def _url(name: str, provider: ProviderSettings) -> httpx.URL:
        try:
            url = httpx.URL(provider.base_url or "")
        except (httpx.InvalidURL, TypeError) as exc:
            msg = f"The model profile '{name}' has an invalid baseUrl: {exc}"
            raise SettingsError(msg) from exc

        if url.scheme not in ("http", "https") or not url.host:
            msg = (
                f"The model profile '{name}' has baseUrl '{provider.base_url}', but "
                "it must be an absolute http or https URL."
            )
            raise SettingsError(msg)

        return url


class PostgresBuilder:
    """PostgresConfig из строки подключения libpq, названной переменной
    окружения: пароль остаётся в окружении и в настройки не попадает."""

    APPLICATION: ClassVar[str] = "boba"

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def config(self, settings: PostgresHistorySettings) -> PostgresConfig:
        dsn = self._env.get(settings.dsn_env)
        if not dsn:
            msg = (
                f"The history store takes its Postgres connection string from the "
                f"environment variable {settings.dsn_env}, but it is not set."
            )
            raise SettingsError(msg)

        try:
            parts = conninfo_to_dict(dsn)
        except psycopg.ProgrammingError as exc:
            msg = f"{settings.dsn_env} is not a valid libpq connection string: {exc}"
            raise SettingsError(msg) from exc

        user = parts.pop("user", None)
        password = parts.pop("password", None)
        if not user:
            msg = (
                f"{settings.dsn_env} names no user; the agent connects to Postgres as "
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

        document: dict[str, Any] = {
            **parts,
            "application_name": self.APPLICATION,
            "auth": auth,
        }
        try:
            return PostgresConfig.model_validate(document)
        except ValidationError as exc:
            msg = f"{settings.dsn_env} does not describe a usable connection: {exc}"
            raise SettingsError(msg) from exc


class McpServerBuilder:
    """McpServerConfig клиента из записи `.mcp.json` и сроков из настроек."""

    BEARER_PREFIX: ClassVar[str] = "bearer "
    AUTHORIZATION: ClassVar[str] = "authorization"

    def __init__(self, client: McpClientJson, env: Mapping[str, str]) -> None:
        self._client = client
        self._env = env

    def config(self, name: str, server: McpServerJson) -> McpServerConfig:
        return McpServerConfig(
            endpoint=self._endpoint(name, server),
            connect_timeout_sec=self._client.connect_timeout_sec,
            call_timeout_sec=self._client.call_timeout_sec,
        )

    def _endpoint(
        self, name: str, server: McpServerJson
    ) -> StdioCommand | HttpEndpoint:
        if server.type is McpTransportJson.SSE:
            msg = (
                f"MCP server '{name}' uses transport 'sse', which this agent does "
                "not support. Use a stdio or streamable http server."
            )
            raise SettingsError(msg)

        if server.type is McpTransportJson.STDIO:
            return self._stdio(name, server)

        return self._http(name, server)

    def _stdio(self, name: str, server: McpServerJson) -> StdioCommand:
        if not server.command:
            msg = f"MCP server '{name}' is a stdio server but has no 'command'."
            raise SettingsError(msg)

        return StdioCommand(
            command=server.command,
            args=tuple(server.args),
            env={**self._env, **server.env},
        )

    def _http(self, name: str, server: McpServerJson) -> HttpEndpoint:
        if not server.url:
            msg = f"MCP server '{name}' is an http server but has no 'url'."
            raise SettingsError(msg)

        try:
            url = httpx.URL(server.url)
        except (httpx.InvalidURL, TypeError) as exc:
            msg = f"MCP server '{name}' has an invalid url: {exc}"
            raise SettingsError(msg) from exc

        if url.scheme not in ("http", "https") or not url.host:
            msg = f"MCP server '{name}' has url '{server.url}', expected http or https."
            raise SettingsError(msg)

        path = url.path
        if not path:
            path = "/"

        return HttpEndpoint(
            scheme=url.scheme,
            host=url.host,
            port=url.port,
            path=path,
            auth=self._auth(name, server.headers),
        )

    def _auth(self, name: str, headers: Mapping[str, str]) -> NoAuth | BearerAuth:
        auth: NoAuth | BearerAuth = NoAuth()
        for header, value in headers.items():
            if header.lower() != self.AUTHORIZATION:
                msg = (
                    f"MCP server '{name}' sets header '{header}'. Only an "
                    "'Authorization: Bearer <token>' header is supported."
                )
                raise SettingsError(msg)

            if not value.lower().startswith(self.BEARER_PREFIX):
                msg = (
                    f"MCP server '{name}' has an Authorization header that is not "
                    "'Bearer <token>'."
                )
                raise SettingsError(msg)

            auth = BearerAuth(token=SecretStr(value[len(self.BEARER_PREFIX) :]))

        return auth


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

    def effective(self) -> EffectiveSettings:
        """Слияние всех слоёв по приоритету."""
        layer = self._defaults()
        for upper in self._layers():
            layer = self._merge.merged(layer, upper)

        return self._effective(layer)

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
            yield self.layer_of(
                self._json.read(source, "--settings"), f"--settings {source}"
            )

        yield self._arguments.layer()
        if self._arguments.policy is not None:
            document = self._json.read_file(Path(self._arguments.policy), "--policy")
            parsed = self._json.parse(document, "--policy", self._arguments.policy)
            yield self.layer_of(parsed, f"--policy {self._arguments.policy}")

    def _file_layer(self, path: Path, what: str) -> Iterator[SettingsLayer]:
        if not path.is_file():
            return

        text = self._json.read_file(path, f"{what} settings")
        document = self._json.parse(text, f"{what} settings", str(path))
        yield self.layer_of(document, f"{what} settings at {path}")

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

        limits = TurnLimits()
        if layer.limits is not None:
            limits = layer.limits.core()

        mcp = layer.mcp
        if mcp is None:
            mcp = McpClientJson()

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
            mcp_servers=self._servers(mcp),
            env=layer.env,
            system_prompt=list(self._system_prompt(layer)),
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

        if arguments.append_system_prompt is not None:
            yield arguments.append_system_prompt

        if arguments.append_system_prompt_file is not None:
            yield self._json.read_file(
                self._cwd / arguments.append_system_prompt_file,
                "the appended system prompt",
            ).strip()

    def _servers(self, client: McpClientJson) -> dict[str, McpServerJson]:
        servers: dict[str, McpServerJson] = {}
        if not self._arguments.strict_mcp_config:
            project = self._cwd / SettingsFile.MCP.value
            if project.is_file():
                text = self._json.read_file(project, ".mcp.json")
                servers.update(self._mcp_json(text, str(project)).mcp_servers)

        for source in self._arguments.mcp_config:
            document = self._json.read(source, "--mcp-config")
            servers.update(self._mcp_json(json.dumps(document), source).mcp_servers)

        chosen: dict[str, McpServerJson] = {}
        for name, server in servers.items():
            if name in client.disabled_servers:
                continue

            if (
                client.enabled_servers is not None
                and name not in client.enabled_servers
            ):
                continue

            chosen[name] = server

        return chosen

    def _mcp_json(self, text: str, source: str) -> McpJson:
        document = self._json.parse(text, ".mcp.json", source)
        try:
            return McpJson.model_validate(document)
        except ValidationError as exc:
            msg = f".mcp.json at {source} does not match the Claude Code format: {exc}"
            raise SettingsError(msg) from exc
