"""Plugin-протокол: декларация конфигурации + сборка артефакта.

Plugin — структурный протокол. Класс плагина должен иметь:
  * `NAME: ClassVar[StrId]` — имя плагина (mount path = `$tool.<NAME>`);
  * classmethod `config()` — возвращает `ObjectSchema` всей секции плагина;
  * classmethod `build(cfg, ctx) -> Out` — фабрика артефакта (`ToolSource`,
    `Pipeline`, ...) из материализованного DTO.

Соглашения convention-уровня:
  * mount path плагина — `$tool.<NAME>` (см. `mount_path_for`);
  * подключение управляется флагом `enable` рядом с конфигом плагина;
    `enable` не объявляется в `config()` — это convention app
    (см. `is_enabled`);
  * default — false (отсутствие `enable` или `enable=false` означает
    «выключен»).

`install_plugins` объединяет всё в один цикл, удобный для composition root.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from boba.coercion import ParseBool
from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath, NameSegment
from boba.declaration import ObjectSchema
from boba.patterns import StrId

__all__ = [
    "ExtensionContext",
    "Plugin",
    "install_plugins",
    "is_enabled",
    "mount_path_for",
]


@dataclass(frozen=True)
class ExtensionContext:
    """Канал общих сервисов для install/build плагина.

    На старте — пустой контейнер. По мере реальных потребностей плагинов
    сюда добавляются shared-сервисы (logger, метрики, observers).
    """


@runtime_checkable
class Plugin(Protocol):
    """Структурный протокол плагина."""

    NAME: ClassVar[StrId]

    @classmethod
    def config(cls) -> ObjectSchema[Any]: ...

    @classmethod
    def build(cls, cfg: Any, ctx: ExtensionContext) -> Any: ...


def mount_path_for(plugin_name: StrId) -> ConfigPath:
    """Convention: каждый плагин монтируется под `$tool.<name>`."""
    return ConfigPath.parse(f"$tool.{plugin_name.to_wire()}")


def is_enabled(bundle: ConfigBundle, mount: ConfigPath) -> bool:
    """Прочитать `<mount>.enable` из FlatConfig. Default: false."""
    lookup = bundle.flat.lookup(mount.join(NameSegment("enable")))
    if not lookup.is_found():
        return False
    try:
        return ParseBool().apply(lookup.value())
    except Exception:
        return False


def install_plugins(
    bundle: ConfigBundle,
    plugin_classes: Iterable[type[Plugin]],
    ctx: ExtensionContext,
) -> Iterable[Any]:
    """Цикл установки: convention-mount → enable → materialize → build.

    Возвращает результаты `plugin_cls.build(cfg, ctx)` для каждого включённого
    плагина. Плагины с `enable != true` пропускаются — их DTO даже не
    материализуется.
    """
    for plugin_cls in plugin_classes:
        mount = mount_path_for(plugin_cls.NAME)
        if not is_enabled(bundle, mount):
            continue
        cfg = bundle.materialize(plugin_cls.config(), mount)
        yield plugin_cls.build(cfg, ctx)
