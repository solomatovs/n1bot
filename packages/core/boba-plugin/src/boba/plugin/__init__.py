"""Plugin-протокол: декларация конфигурации + сборка артефактов.

Plugin — структурный протокол, дженерик по `TConfig` (DTO секции) и
`TToolSource` (тип артефакта). Класс плагина должен иметь:
  * `NAME: ClassVar[StrId]` — имя плагина (mount path = `tool.<NAME>`);
  * classmethod `build(cfg, ctx)` → `Iterable[TToolSource]` — итератор
    артефактов. Один плагин может вернуть несколько источников (например,
    MCP-плагин — по одному `ToolSource` на сервер).

Тип DTO секции (`TConfig`) резолвится из дженерик-параметра базы
`Plugin[TConfig, ...]` и материализуется через `ConfigBundle.get`. Сам
плагин не объявляет схему — `schema_from_dataclass` строит её по
аннотациям DTO (`Annotated[...]` + `@schema(invariants=...)` при
необходимости).

Соглашения convention-уровня:
  * mount path плагина — `tool.<NAME>` (см. `config_path`);
  * подключение управляется флагом `enable` рядом с конфигом плагина;
    `enable` не объявляется в DTO — это convention app (см. `is_enabled`);
  * default — false (отсутствие `enable` или `enable=false` означает
    «выключен»).

`install_plugins` объединяет всё в один цикл, удобный для composition root.
"""

from __future__ import annotations

import typing
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, TypeVar, runtime_checkable

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath, NameSegment
from boba.patterns import StrId
from boba.schema.coercion import ParseBool

__all__ = [
    "ExtensionContext",
    "Plugin",
    "config_path",
    "install_plugins",
    "is_enabled",
    "resolve_config_type",
]

TConfig_contra = TypeVar("TConfig_contra", contravariant=True)
TToolSource_co = TypeVar("TToolSource_co", covariant=True)


@dataclass(frozen=True)
class ExtensionContext:
    """Канал общих сервисов для install/build плагина.

    На старте — пустой контейнер. По мере реальных потребностей плагинов
    сюда добавляются shared-сервисы (logger, метрики, observers,
    async-runtime).
    """


@runtime_checkable
class Plugin(Protocol[TConfig_contra, TToolSource_co]):
    """Структурный протокол плагина.

    Дженерик по `TConfig` (DTO секции) и `TToolSource` (тип артефакта).
    Concrete-плагин: `class HtmlPlugin(Plugin[HtmlPluginConfig, ToolSource])`.
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
    """Извлечь TConfig из `class Foo(Plugin[Cfg, ToolSource])`.

    Берётся первый generic-base, чей origin — `Plugin`. Если такого нет
    или TConfig — TypeVar (плагин-абстракция), бросается TypeError.
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
    msg = (
        f"{plugin_cls.__name__} должен наследоваться от "
        f"Plugin[TConfig, TToolSource]"
    )
    raise TypeError(msg)


T = TypeVar("T")


def install_plugins(
    bundle: ConfigBundle,
    plugin_classes: Iterable[type[Plugin[Any, T]]],
    ctx: ExtensionContext,
) -> Iterable[T]:
    """Цикл установки: convention-mount → enable → materialize → build.

    Уплощает `Iterable[Iterable[T]]` от каждого `build()` в общий поток `T`.
    Плагины с `enable != true` пропускаются — их DTO даже не материализуется.
    """
    for plugin_cls in plugin_classes:
        p = config_path(plugin_cls.NAME)
        if not is_enabled(bundle, p):
            continue

        cfg_type = resolve_config_type(plugin_cls)
        yield from plugin_cls.build(bundle.get(cfg_type, p), ctx)
