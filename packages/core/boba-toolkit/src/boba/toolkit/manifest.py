"""Плагин инструментов по спецификации v1: объявление автора и манифест.

Автор плагина объявляет его объектом ToolPlugin: инструменты, хранилище,
прогревы зиготы и сеть. Программа плагина строит из объявления манифест
PluginManifest (ManifestBuilder) и отдаёт его серверу JSON-ом; сервер читает
только манифест и кода плагина не импортирует.
Здесь же роли служебных параметров тела (InjectedRole) и базы моделей, по
которым построитель узнаёт роль: контекст вызова (ContextValue) и профиль
соединения (ConnectionProfile).

Ошибки:
ManifestError — объявление плагина не укладывается в спецификацию:
    injected-параметр не модель, соединение без вида, профиль соединения
    внутри списка или словаря конфига, два профиля разного вида по одному
    пути; секция плагина не подходит модели его хранилища.
pydantic.ValidationError — манифест не проходит модель PluginManifest,
    в том числе чужой номер спецификации.
"""

from __future__ import annotations

import hashlib
from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.fields import FieldInfo

from boba.core.validation import ValidationText
from boba.toolkit.calls import CallSchema, CallViews, FieldMarks, SchemaMark
from boba.toolkit.dag import ToolHints
from boba.toolkit.facade import PayloadTool, WarmupHook
from boba.toolkit.ports import StreamPorts, StreamSpec

__all__ = [
    "ConfigConnection",
    "ConnectionProfile",
    "ContextValue",
    "InjectedRole",
    "ManifestBuilder",
    "ManifestError",
    "ManifestReader",
    "PluginManifest",
    "PluginSetup",
    "PluginStorage",
    "ToolDecl",
    "ToolPlugin",
    "WarmupDecl",
]

CfgT = TypeVar("CfgT", bound=BaseModel)


class ManifestError(Exception):
    """Объявление плагина не укладывается в спецификацию манифеста."""


class InjectedRole(StrEnum):
    """Чем хост наполняет служебный параметр тела.

    CONFIG — секция [tool.<id>] плагина; SUBJECT, SCOPE и WORKSPACE_ROOT —
    контекст вызова: от чьего имени, в какой области, над каким workspace;
    PROGRESS — отчёты о ходе работы, их строит сам гость.
    """

    CONFIG = "config"
    SUBJECT = "subject"
    SCOPE = "scope"
    WORKSPACE_ROOT = "workspace_root"
    PROGRESS = "progress"


class ContextValue(BaseModel):
    """База моделей контекста вызова, которые тело получает injected-параметром.

    Наследники — Subject, Scope и WorkspaceRoot; каждый называет свою роль
    ROLE, и по ней построитель манифеста объявляет параметр, а хост
    подставляет значение на вызов.
    """

    ROLE: ClassVar[InjectedRole]


class ConnectionProfile(BaseModel):
    """База профилей соединений: по ней построитель манифеста узнаёт
    параметр-соединение и профиль внутри конфига плагина.

    Наследник (ConnectionBase и типы пакетов-владельцев) сужает поле kind
    до Literal своего вида: вид и уходит в манифест.
    """

    KIND_FIELD: ClassVar[str] = "kind"


class PluginStorage(Protocol):
    """Хранилище плагина: setup создаёт его таблицы; повтор безвреден.
    Реализуют KbSchema и DescriberStore."""

    @abstractmethod
    async def setup(self) -> None: ...


@dataclass(frozen=True)
class PluginSetup(Generic[CfgT]):
    """Подготовка хранилища плагина: модель его секции и хранилище по ней.

    Программа плагина в подкоманде setup разбирает секцию моделью config и
    зовёт storage(cfg).setup(); сервер зовёт подкоманду на старте, когда
    манифест объявил setup.
    """

    config: type[CfgT]
    storage: Callable[[CfgT], PluginStorage]

    def storage_of(self, plugin: str, section: Mapping[str, Any]) -> PluginStorage:
        """Хранилище по секции section плагина plugin.

        Ошибки:
        ManifestError — секция не подходит модели config.
        """
        try:
            cfg = self.config.model_validate(dict(section))
        except ValidationError as exc:
            msg = (
                f"[tool.{plugin}] does not fit {self.config.__name__}: "
                f"{ValidationText.of(exc)}"
            )
            raise ManifestError(msg) from None

        return self.storage(cfg)


@dataclass(frozen=True)
class ToolPlugin:
    """Объявление плагина автором: то, из чего программа плагина строит манифест.

    id — единственное имя плагина: имя секции [tool.<id>], файла конфига
    <id>.toml и entry point'а группы GROUP, по которому программа плагина
    находит объявление в своём процессе.
    """

    GROUP: ClassVar[str] = "boba.tools"

    id: str
    tools: tuple[PayloadTool, ...]
    setup: PluginSetup[Any] | None = None
    warmups: tuple[WarmupHook, ...] = ()
    network: bool = False

    def joined(self, other: ToolPlugin) -> ToolPlugin:
        """Этот плагин с инструментами и прогревами other: так стенд тестов
        поднимает одну зиготу над объявлениями нескольких модулей."""
        return replace(
            self,
            tools=(*self.tools, *other.tools),
            warmups=(*self.warmups, *other.warmups),
        )


class ToolDecl(BaseModel):
    """Инструмент в манифесте: всё, что сервер знает о нём, данными.

    input_schema — JSON Schema аргументов модели (properties, required,
    $defs); служебных параметров в ней нет. injected — роли служебных
    параметров тела, connections — параметры-соединения и их вид, stream —
    порты каналов и барьер группы, results — виды результата.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str
    hints: ToolHints
    input_schema: dict[str, Any]
    injected: dict[str, InjectedRole] = Field(default_factory=dict)
    connections: dict[str, str] = Field(default_factory=dict)
    stream: StreamSpec = Field(default_factory=StreamSpec)
    results: tuple[str, ...] = ()

    def properties(self) -> Mapping[str, Any]:
        """Аргументы модели: имя → схема."""
        declared = self.input_schema.get(SchemaMark.PROPERTIES)
        if not isinstance(declared, Mapping):
            return {}

        return declared

    def config_params(self) -> tuple[str, ...]:
        """Параметры, которым хост подаёт секцию плагина."""
        names: list[str] = []
        for name, role in self.injected.items():
            if role is InjectedRole.CONFIG:
                names.append(name)

        return tuple(names)


class ConfigConnection(BaseModel):
    """Профиль соединения внутри секции плагина: JSON-указатель и вид.

    По нему хост заменяет keytab-секцию профиля билетом вызова, прежде чем
    отдать секцию телу.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SEPARATOR: ClassVar[str] = "/"

    pointer: str = Field(pattern=r"^(/[^/]+)+$")
    kind: str = Field(min_length=1)

    def path(self) -> tuple[str, ...]:
        """Ключи от корня секции до профиля."""
        return tuple(self.pointer.split(self.SEPARATOR)[1:])


class WarmupDecl(BaseModel):
    """Прогрев зиготы в манифесте: модуль и имя хука."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str = Field(min_length=1)
    hook: str = Field(min_length=1)


class PluginManifest(BaseModel):
    """Манифест плагина v1: то, что программа плагина печатает на `manifest`.

    Сервер знает ровно одну версию спецификации SPEC: манифест другой версии
    отвергается, и в тексте названы обе. digest() — отпечаток манифеста:
    зигота называет его при готовности, и подменённый на ходу образ виден
    хосту.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SPEC: ClassVar[int] = 1

    spec: int
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    setup: bool
    warmups: tuple[WarmupDecl, ...] = ()
    network: bool
    connections_in_config: tuple[ConfigConnection, ...] = ()
    tools: tuple[ToolDecl, ...]

    @model_validator(mode="before")
    @classmethod
    def _known_spec(cls, raw: object) -> object:
        if not isinstance(raw, Mapping):
            return raw

        spec = raw.get("spec")
        if spec != cls.SPEC:
            msg = (
                f"plugin manifest spec {spec!r} is not supported: this server "
                f"reads spec {cls.SPEC} only"
            )
            raise ValueError(msg)

        return raw

    def digest(self) -> str:
        """sha256 канонического JSON манифеста."""
        canonical = self.model_dump_json().encode("utf-8")

        return hashlib.sha256(canonical).hexdigest()


class ManifestReader:
    """Чтение манифеста сервером: JSON подкоманды manifest → PluginManifest.

    Создаёт его способ запуска плагинов сервера; текст отказа называет
    нарушения без значений манифеста.
    """

    def read(self, raw: bytes) -> PluginManifest:
        """Манифест из JSON raw.

        Ошибки:
        ManifestError — raw не манифест спецификации SPEC.
        """
        try:
            return PluginManifest.model_validate_json(raw)
        except ValidationError as exc:
            msg = (
                f"{len(raw)} bytes are not a plugin manifest of spec "
                f"{PluginManifest.SPEC}: {ValidationText.of(exc)}"
            )
            raise ManifestError(msg) from None


class ManifestBuilder:
    """Объявление плагина ToolPlugin → манифест PluginManifest.

    Единственное место, где python-типы плагина становятся данными:
    программа плагина зовёт manifest() на подкоманде manifest и digest для
    зиготы, тесты и стенды — tool() для инструментов, которые запускают сами.
    config_models() — модели секции плагина, которыми программа проверяет
    конфиг до манифеста.
    """

    def __init__(self) -> None:
        self._schemas = CallSchema()
        self._views = CallViews()

    def manifest(self, plugin: ToolPlugin, version: str) -> PluginManifest:
        tools: list[ToolDecl] = []
        for payload in plugin.tools:
            tools.append(self.tool(payload))

        warmups: list[WarmupDecl] = []
        for hook in plugin.warmups:
            warmups.append(WarmupDecl(module=hook.module, hook=hook.name))

        return PluginManifest(
            spec=PluginManifest.SPEC,
            id=plugin.id,
            version=version,
            setup=plugin.setup is not None,
            warmups=tuple(warmups),
            network=plugin.network,
            connections_in_config=self._config_connections(plugin),
            tools=tuple(tools),
        )

    def tool(self, payload: PayloadTool) -> ToolDecl:
        """Инструмент манифеста по объявлению фасада @tool."""
        schema = payload.args_schema

        injected: dict[str, InjectedRole] = {}
        connections: dict[str, str] = {}
        model_fields: dict[str, FieldInfo] = {}
        for name, field in schema.model_fields.items():
            if StreamPorts.is_progress(field.annotation):
                injected[name] = InjectedRole.PROGRESS
                continue

            if StreamPorts.is_group(field.annotation):
                continue

            if StreamPorts.is_port(field.annotation):
                continue

            if FieldMarks.connection(field):
                connections[name] = self._kind_of(payload.name, name, field.annotation)
                continue

            if FieldMarks.injected(field):
                injected[name] = self._role_of(payload.name, name, field.annotation)
                continue

            model_fields[name] = field

        return ToolDecl(
            name=payload.name,
            description=payload.description,
            hints=payload.hints,
            input_schema=self._input_schema(payload, model_fields),
            injected=injected,
            connections=connections,
            stream=StreamSpec.of_schema(schema),
            results=payload.results,
        )

    def config_models(self, plugin: ToolPlugin) -> tuple[type[BaseModel], ...]:
        """Модели секции плагина: injected-конфиги инструментов, прогревов и
        хранилища, без повторов."""
        models: list[type[BaseModel]] = []
        for model in self._declared_models(plugin):
            if model in models:
                continue

            models.append(model)

        return tuple(models)

    def unfit(self, plugin: ToolPlugin, section: Mapping[str, Any]) -> Iterator[str]:
        """Почему секция section не подходит моделям конфига плагина: строка
        на модель; пусто — подходит всем."""
        for model in self.config_models(plugin):
            try:
                model.model_validate(dict(section))
            except ValidationError as exc:
                yield (
                    f"[tool.{plugin.id}] does not fit {model.__name__}: "
                    f"{ValidationText.of(exc)}"
                )

    def _declared_models(self, plugin: ToolPlugin) -> Iterator[type[BaseModel]]:
        for payload in plugin.tools:
            yield from self._config_annotations(payload)

        for hook in plugin.warmups:
            yield hook.config_model

        if plugin.setup is not None:
            yield plugin.setup.config

    def _config_annotations(self, payload: PayloadTool) -> Iterator[type[BaseModel]]:
        for name, field in payload.args_schema.model_fields.items():
            if StreamPorts.is_progress(field.annotation):
                continue

            if StreamPorts.is_group(field.annotation):
                continue

            if StreamPorts.is_port(field.annotation):
                continue

            if FieldMarks.connection(field):
                continue

            if not FieldMarks.injected(field):
                continue

            role = self._role_of(payload.name, name, field.annotation)
            if role is not InjectedRole.CONFIG:
                continue

            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                yield annotation

    def _input_schema(
        self, payload: PayloadTool, fields: Mapping[str, FieldInfo]
    ) -> dict[str, Any]:
        """Схема аргументов модели: аргумент-код назван типом содержимого,
        значение, которое нельзя писать в лог, — пометкой NOT_LOGGED."""
        built = self._schemas.of(payload.name, "", fields)
        views = self._views.of(payload.args_schema)
        typed = self._views.typed(built, views)

        properties = dict(typed.get(SchemaMark.PROPERTIES, {}))
        for name, field in fields.items():
            if not FieldMarks.not_logged(field):
                continue

            properties[name] = {**properties[name], SchemaMark.NOT_LOGGED.value: True}

        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required := typed.get("required"):
            schema["required"] = list(required)

        if defs := typed.get("$defs"):
            schema["$defs"] = dict(defs)

        return schema

    @staticmethod
    def _role_of(tool: str, param: str, annotation: object) -> InjectedRole:
        """Роль injected-параметра по его модели."""
        if not isinstance(annotation, type):
            msg = (
                f"tool {tool!r}: injected parameter {param!r} must be annotated "
                f"with a pydantic model class, got {annotation!r}"
            )
            raise ManifestError(msg)

        if issubclass(annotation, ContextValue):
            return annotation.ROLE

        if issubclass(annotation, BaseModel):
            return InjectedRole.CONFIG

        msg = (
            f"tool {tool!r}: injected parameter {param!r} must subclass pydantic "
            f"BaseModel, got {annotation.__name__}"
        )
        raise ManifestError(msg)

    def _kind_of(self, tool: str, param: str, annotation: object) -> str:
        """Вид соединения параметра по модели его профиля."""
        if not isinstance(annotation, type):
            msg = (
                f"tool {tool!r}: connection parameter {param!r} must be annotated "
                f"with a connection profile model, got {annotation!r}"
            )
            raise ManifestError(msg)

        if not issubclass(annotation, ConnectionProfile):
            msg = (
                f"tool {tool!r}: connection parameter {param!r} is annotated with "
                f"{annotation.__name__}, which is not a connection profile"
            )
            raise ManifestError(msg)

        return self._profile_kind(annotation)

    @staticmethod
    def _profile_kind(model: type[ConnectionProfile]) -> str:
        """Вид профиля: единственная строка Literal его поля kind."""
        field = model.model_fields.get(ConnectionProfile.KIND_FIELD)
        if field is None:
            msg = f"connection profile {model.__name__} has no kind field"
            raise ManifestError(msg)

        values = get_args(field.annotation)
        if get_origin(field.annotation) is not Literal:
            msg = (
                f"connection profile {model.__name__} must narrow kind to a "
                f"Literal of its kind, got {field.annotation!r}"
            )
            raise ManifestError(msg)

        if len(values) != 1:
            msg = (
                f"connection profile {model.__name__} must declare exactly one "
                f"kind, got {values!r}"
            )
            raise ManifestError(msg)

        return str(values[0])

    def _config_connections(self, plugin: ToolPlugin) -> tuple[ConfigConnection, ...]:
        """Профили соединений во всех моделях секции плагина."""
        found: dict[str, ConfigConnection] = {}
        for model in self.config_models(plugin):
            for connection in self._profiles(model, ()):
                known = found.get(connection.pointer)
                if known is None:
                    found[connection.pointer] = connection
                    continue

                if known.kind != connection.kind:
                    msg = (
                        f"plugin {plugin.id!r}: config path {connection.pointer} "
                        f"holds a {known.kind} profile in one model and a "
                        f"{connection.kind} profile in another"
                    )
                    raise ManifestError(msg)

        return tuple(found.values())

    def _profiles(
        self, model: type[BaseModel], path: tuple[str, ...]
    ) -> Iterator[ConfigConnection]:
        for name, field in model.model_fields.items():
            yield from self._annotated(field.annotation, (*path, name))

    def _annotated(
        self, annotation: object, path: tuple[str, ...]
    ) -> Iterator[ConfigConnection]:
        """Профили под аннотацией поля: модель, необязательная модель или
        союз моделей; профиль в списке или словаре спецификация не берёт."""
        origin = get_origin(annotation)
        if origin is Annotated:
            yield from self._annotated(get_args(annotation)[0], path)
            return

        if origin in (Union, UnionType):
            for member in get_args(annotation):
                if member is NoneType:
                    continue

                yield from self._annotated(member, path)
            return

        if origin is not None:
            self._refuse_nested(annotation, path)
            return

        if not isinstance(annotation, type):
            return

        if issubclass(annotation, ConnectionProfile):
            pointer = ConfigConnection.SEPARATOR + ConfigConnection.SEPARATOR.join(path)
            yield ConfigConnection(pointer=pointer, kind=self._profile_kind(annotation))
            return

        if issubclass(annotation, BaseModel):
            yield from self._profiles(annotation, path)

    def _refuse_nested(self, annotation: object, path: Sequence[str]) -> None:
        """Контейнер с профилем соединения внутри: указатель на него в v1 не
        строится, и хост не выдал бы такому профилю билет."""
        for member in get_args(annotation):
            if not isinstance(member, type):
                continue

            if not issubclass(member, ConnectionProfile):
                continue

            pointer = "/".join(path)
            msg = (
                f"config field /{pointer} holds connection profiles inside "
                f"{annotation!r}: spec {PluginManifest.SPEC} takes a profile only "
                "as a model field"
            )
            raise ManifestError(msg)
