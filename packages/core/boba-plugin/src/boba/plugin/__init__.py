"""
Plugin-протокол: декларация конфигурации + сборка артефактов.

Plugin — структурный протокол, дженерик по `TConfig` (DTO секции) и
`TToolSource` (тип артефакта).

Класс плагина должен иметь:
  * `NAME: ClassVar[StrId]` — имя плагина (mount path = `tool.<NAME>`);
  * classmethod `build(cfg, ctx)` → `Iterable[TToolSource]` - итератор артефактов

Один плагин может вернуть несколько источников
например, MCP-плагин — по одному `ToolSource` на сервер

`TConfig` DTO резолвится из дженерик-параметра базы `Plugin[TConfig, ...]`
материализуется через `ConfigBundle.get`.

Плагин строит схему TConfig через аннотации DTO:
 - `Annotated[...]` блоки
 - `@schema(invariants=...)`

Соглашения плагинов:

  * `tool.<NAME>` - это секция для конфигурирования плагина, см. `def config_path()`

  * Каждый плагин включается отдельно и управляется флагом `enable` из конфигураций
    У плагина всегда есть собственная секция конфига - `tool.<NAME>`,
    где обязательно будет произведена попытка считать флаг `enable`
    Если этот флаг true, значит плагин включен и работает, иначе нет
    `enable` не объявляется в DTO конфига плагина, попытка его получить происходит
    в инфраструктурном слое
"""

from __future__ import annotations

import typing
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar, Protocol, TypeVar, cast, runtime_checkable

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath, NameSegment
from boba.patterns import StrId
from boba.schema.coercion import ParseBool

__all__ = [
    "ExtensionContext",
    "MissingExtensionError",
    "Plugin",
    "config_path",
    "install_plugins",
    "is_enabled",
    "resolve_config_type",
]

TConfig_contra = TypeVar("TConfig_contra", contravariant=True)
TToolSource_co = TypeVar("TToolSource_co", covariant=True)
T = TypeVar("T")


class MissingExtensionError(KeyError):
    """Запрошен extension, не зарегистрированный в `ExtensionContext`."""

    def __init__(self, key: type) -> None:
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        return f"extension {self.key.__name__!r} not registered"


class ExtensionContext:
    """
    Типизированный реестр shared-сервисов для install/build плагина

    Позволяет Plugin'у запросить зависимость по типу
    По сути является неким DI для плагинов

    Незарегистрированный тип = `MissingExtensionError`

    Иммутабелен после создания.
    """

    __slots__ = ("_providers",)

    def __init__(
        self,
        providers: Mapping[type, object] | None = None,
    ) -> None:
        self._providers: dict[type, object] = dict(providers or {})

    def get(self, key: type[T]) -> T:
        """Вернуть extension по типу; иначе `MissingExtensionError`."""
        try:
            return cast(T, self._providers[key])
        except KeyError as e:
            raise MissingExtensionError(key) from e

    def has(self, key: type) -> bool:
        """Проверить, зарегистрирован ли extension данного типа."""
        return key in self._providers


@runtime_checkable
class Plugin(Protocol[TConfig_contra, TToolSource_co]):
    """
    Структурный протокол плагина

    Дженерик:
    `TConfig` - DTO секции (конфиг секции)
    `TToolSource` - тип ToolSource
        бывают разные, типпа StaticToolSource, McpToolSource, ...

    пример: `class HtmlPlugin(Plugin[HtmlPluginConfig, ToolSource])`
    """

    NAME: ClassVar[StrId]

    @classmethod
    def build(
        cls,
        cfg: TConfig_contra,
        ctx: ExtensionContext,
    ) -> Iterable[TToolSource_co]: ...


def config_path(plugin_name: StrId) -> ConfigPath:
    """Convention: каждый плагин монтируется под `tool.<name>`."""
    return ConfigPath.parse("tool").join(NameSegment(plugin_name.to_wire()))


def is_enabled(bundle: ConfigBundle, mount: ConfigPath) -> bool:
    """Прочитать `<mount>.enable` из FlatConfig. Default: false."""
    lookup = bundle.flat.lookup(mount.join(NameSegment("enable")))
    if not lookup.is_found():
        return False
    try:
        return ParseBool().apply(lookup.value())
    except Exception:
        return False


def resolve_config_type(plugin_cls: type[Plugin[Any, Any]]) -> type:
    """
    Извлечь TConfig из `class Foo(Plugin[Cfg, ToolSource])`

    Берётся первый generic-base, чей origin — `Plugin`.
    Если такого нет или TConfig — TypeVar (плагин-абстракция), бросается TypeError.
    """
    for base in getattr(plugin_cls, "__orig_bases__", ()):
        if typing.get_origin(base) is Plugin:
            args = typing.get_args(base)
            if not args:
                continue
            cfg = args[0]
            if isinstance(cfg, TypeVar):
                msg = (
                    f"{plugin_cls.__name__}: TConfig — TypeVar, "
                    f"плагин не параметризован конкретным DTO"
                )
                raise TypeError(msg)
            if not isinstance(cfg, type):
                msg = (
                    f"{plugin_cls.__name__}: TConfig должен быть конкретным "
                    f"типом, получено {cfg!r}"
                )
                raise TypeError(msg)
            return cfg
    msg = f"{plugin_cls.__name__} должен наследоваться от Plugin[TConfig, TToolSource]"
    raise TypeError(msg)


def install_plugins(
    bundle: ConfigBundle,
    plugin_classes: Iterable[type[Plugin[Any, T]]],
    ctx: ExtensionContext,
) -> Iterable[T]:
    """
    Цикл установки плагина: convention-mount → enable → materialize → build.

    Плагины с `enable != true` пропускаются
    их DTO даже не материализуется
    """
    for plugin_cls in plugin_classes:
        p = config_path(plugin_cls.NAME)
        if not is_enabled(bundle, p):
            continue

        cfg_type = resolve_config_type(plugin_cls)
        yield from plugin_cls.build(bundle.get(cfg_type, p), ctx)
