"""Plugin Protocol + helpers (config_path, is_enabled, install_plugins,
resolve_config_type)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pytest

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath
from boba.config.source.dict import DictSource
from boba.plugin import (
    ExtensionContext,
    MissingExtensionError,
    Plugin,
    config_path,
    install_plugins,
    is_enabled,
    resolve_config_type,
)
from boba.schema.coercion import (
    Default,
    ParseBool,
    ParseInt,
    ParseString,
    Required,
)
from boba.schema.value import BoolValue, IntValue, StringValue


@dataclass(frozen=True)
class _SearchCfg:
    base_url: Annotated[str, "URL поиска.", Required(), ParseString()]
    limit: Annotated[int, "Лимит.", ParseInt()] = 10


@dataclass(frozen=True)
class _BuiltSearch:
    cfg: _SearchCfg
    ctx: ExtensionContext


class _SearchPlugin(Plugin[_SearchCfg, _BuiltSearch]):
    NAME: ClassVar[str] = "search"

    @classmethod
    def build(
        cls,
        cfg: _SearchCfg,
        ctx: ExtensionContext,
    ) -> Iterable[_BuiltSearch]:
        yield _BuiltSearch(cfg=cfg, ctx=ctx)


# mount path / enable


def test_mount_path_for_uses_tool_prefix():
    assert config_path("search") == ConfigPath.parse("tool.search")
    assert config_path("confluence_page") == ConfigPath.parse(
        "tool.confluence_page",
    )


def test_is_enabled_default_false_when_absent():
    bundle = ConfigBundle.from_sources([DictSource({})])
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is False


def test_is_enabled_true_when_explicit_true():
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): BoolValue(True)})],
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is True


def test_is_enabled_false_when_explicit_false():
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): BoolValue(False)})],
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is False


def test_is_enabled_parses_string_true():
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): StringValue("true")})],
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is True


def test_is_enabled_garbage_string_treated_as_false():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {ConfigPath.parse("tool.search.enable"): StringValue("not-a-bool")},
            ),
        ],
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is False


# resolve_config_type


def test_resolve_config_type_returns_tconfig():
    assert resolve_config_type(_SearchPlugin) is _SearchCfg


def test_resolve_config_type_raises_without_plugin_base():
    class _NotAPlugin:
        NAME: ClassVar[str] = "nope"

    with pytest.raises(TypeError, match="должен наследоваться"):
        resolve_config_type(_NotAPlugin)  # type: ignore[arg-type]


def test_resolve_config_type_raises_when_tconfig_is_typevar():
    """Абстрактный плагин (без конкретного DTO) не должен материализовываться."""
    from typing import TypeVar

    TCfg = TypeVar("TCfg")

    class _Abstract(Plugin[TCfg, Any]):  # type: ignore[type-var]
        NAME: ClassVar[str] = "abstract"

        @classmethod
        def build(cls, cfg: TCfg, ctx: ExtensionContext) -> Iterable[Any]:  # type: ignore[type-var]
            return ()

    with pytest.raises(TypeError, match="TypeVar"):
        resolve_config_type(_Abstract)


# install_plugins


def test_install_plugins_skips_disabled():
    bundle = ConfigBundle.from_sources([DictSource({})])
    ctx = ExtensionContext()
    artifacts = list(install_plugins(bundle, [_SearchPlugin], ctx))
    assert artifacts == []


def test_install_plugins_materializes_and_builds_when_enabled():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.search.enable"): BoolValue(True),
                    ConfigPath.parse("tool.search.base_url"): StringValue(
                        "https://example.com",
                    ),
                    ConfigPath.parse("tool.search.limit"): IntValue(50),
                },
            ),
        ],
    )
    ctx = ExtensionContext()
    artifacts = list(install_plugins(bundle, [_SearchPlugin], ctx))
    assert len(artifacts) == 1
    built = artifacts[0]
    assert isinstance(built, _BuiltSearch)
    assert built.cfg == _SearchCfg(base_url="https://example.com", limit=50)
    assert built.ctx is ctx


def test_install_plugins_disabled_plugin_dto_is_not_materialized():
    """Если плагин выключен — required-поле не падает (DTO не строится)."""
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): BoolValue(False)})],
    )
    ctx = ExtensionContext()
    assert list(install_plugins(bundle, [_SearchPlugin], ctx)) == []


def test_install_plugins_iterates_multiple():
    class _OtherPlugin(Plugin[_SearchCfg, _BuiltSearch]):
        NAME: ClassVar[str] = "other"

        @classmethod
        def build(
            cls,
            cfg: _SearchCfg,
            ctx: ExtensionContext,
        ) -> Iterable[_BuiltSearch]:
            yield _BuiltSearch(cfg=cfg, ctx=ctx)

    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.search.enable"): BoolValue(True),
                    ConfigPath.parse("tool.search.base_url"): StringValue(
                        "https://search",
                    ),
                    ConfigPath.parse("tool.other.enable"): BoolValue(True),
                    ConfigPath.parse("tool.other.base_url"): StringValue(
                        "https://other",
                    ),
                },
            ),
        ],
    )
    ctx = ExtensionContext()
    artifacts = list(install_plugins(bundle, [_SearchPlugin, _OtherPlugin], ctx))
    assert len(artifacts) == 2
    urls = sorted(a.cfg.base_url for a in artifacts)
    assert urls == ["https://other", "https://search"]


# Protocol structural-check


def test_search_plugin_satisfies_runtime_protocol():
    """Plugin — runtime_checkable Protocol; проверяем наличие членов."""
    assert isinstance(_SearchPlugin, type)
    assert hasattr(_SearchPlugin, "NAME")
    assert callable(getattr(_SearchPlugin, "build", None))


# ExtensionContext — typed lookup


class _RegistryA:
    pass


class _RegistryB:
    pass


def test_extension_context_get_returns_registered_instance():
    """`get(Type)` возвращает зарегистрированный объект ровно по типу-ключу."""
    a, b = _RegistryA(), _RegistryB()
    ctx = ExtensionContext({_RegistryA: a, _RegistryB: b})
    assert ctx.get(_RegistryA) is a
    assert ctx.get(_RegistryB) is b


def test_extension_context_get_missing_raises():
    """Незарегистрированный тип → `MissingExtensionError` с понятным сообщением."""
    ctx = ExtensionContext()
    with pytest.raises(MissingExtensionError) as exc:
        ctx.get(_RegistryA)
    assert exc.value.key is _RegistryA
    assert "_RegistryA" in str(exc.value)


def test_extension_context_has_reflects_registration():
    """`has(Type)` — true только для зарегистрированных типов."""
    ctx = ExtensionContext({_RegistryA: _RegistryA()})
    assert ctx.has(_RegistryA) is True
    assert ctx.has(_RegistryB) is False


def test_extension_context_no_args_is_empty_bag():
    """`ExtensionContext()` без аргументов — пустой реестр (back-compat)."""
    ctx = ExtensionContext()
    assert ctx.has(_RegistryA) is False
    with pytest.raises(MissingExtensionError):
        ctx.get(_RegistryA)


# Re-export, чтобы pyright не считал импорт неиспользованным.
_ = ParseBool
_ = Default
_ = Plugin
