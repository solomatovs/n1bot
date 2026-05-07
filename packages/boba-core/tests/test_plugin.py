"""Plugin Protocol + helpers (mount_path_for, is_enabled, install_plugins)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, ParseBool, ParseInt, ParseString
from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath, ConfigSource
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.plugin import (
    ExtensionContext,
    Plugin,
    install_plugins,
    is_enabled,
    mount_path_for,
)
from boba.value import BoolValue, ConfigValue, IntValue, StringValue


class _DictSource(ConfigSource):
    def __init__(self, values: Mapping[ConfigPath, ConfigValue]) -> None:
        self._v = values

    def name(self) -> str:
        return "dict"

    def priority(self) -> int:
        return 100

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        return dict(self._v)


def _path(s: str) -> ConfigPath:
    return ConfigPath.parse(s)


@dataclass(frozen=True)
class _SearchCfg:
    base_url: str
    limit: int


_SEARCH_SCHEMA: ObjectSchema[_SearchCfg] = ObjectSchema(
    fields=[
        FieldSpec("base_url", ChainCoercer(ParseString()), required=True),
        FieldSpec("limit", ChainCoercer(Default(10), ParseInt())),
    ],
    factory=_SearchCfg,
)


@dataclass(frozen=True)
class _BuiltSearch:
    cfg: _SearchCfg
    ctx: ExtensionContext


class _SearchPlugin:
    NAME: ClassVar[StrId] = StrId("search")

    @classmethod
    def config(cls) -> ObjectSchema[_SearchCfg]:
        return _SEARCH_SCHEMA

    @classmethod
    def build(cls, cfg: _SearchCfg, ctx: ExtensionContext) -> _BuiltSearch:
        return _BuiltSearch(cfg=cfg, ctx=ctx)


def test_mount_path_for_uses_tool_prefix():
    assert mount_path_for(StrId("search")) == _path("$tool.search")
    assert mount_path_for(StrId("confluence_page")) == _path("$tool.confluence_page")


def test_is_enabled_default_false_when_absent():
    bundle = ConfigBundle.from_sources([_DictSource({})])
    assert is_enabled(bundle, _path("$tool.search")) is False


def test_is_enabled_true_when_explicit_true():
    bundle = ConfigBundle.from_sources(
        [_DictSource({_path("$tool.search.enable"): BoolValue(True)})]
    )
    assert is_enabled(bundle, _path("$tool.search")) is True


def test_is_enabled_false_when_explicit_false():
    bundle = ConfigBundle.from_sources(
        [_DictSource({_path("$tool.search.enable"): BoolValue(False)})]
    )
    assert is_enabled(bundle, _path("$tool.search")) is False


def test_is_enabled_parses_string_true():
    bundle = ConfigBundle.from_sources(
        [_DictSource({_path("$tool.search.enable"): StringValue("true")})]
    )
    assert is_enabled(bundle, _path("$tool.search")) is True


def test_is_enabled_garbage_string_treated_as_false():
    bundle = ConfigBundle.from_sources(
        [_DictSource({_path("$tool.search.enable"): StringValue("not-a-bool")})]
    )
    assert is_enabled(bundle, _path("$tool.search")) is False


# --- install_plugins ---


def test_install_plugins_skips_disabled():
    bundle = ConfigBundle.from_sources([_DictSource({})])
    ctx = ExtensionContext()
    artifacts = list(install_plugins(bundle, [_SearchPlugin], ctx))
    assert artifacts == []


def test_install_plugins_materializes_and_builds_when_enabled():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("$tool.search.enable"): BoolValue(True),
                    _path("$tool.search.base_url"): StringValue("https://example.com"),
                    _path("$tool.search.limit"): IntValue(50),
                }
            )
        ]
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
    # Никакого base_url в bundle, плагин выключен → не должна подниматься
    # FieldPathMissingError, плагин просто пропускается.
    bundle = ConfigBundle.from_sources(
        [_DictSource({_path("$tool.search.enable"): BoolValue(False)})]
    )
    ctx = ExtensionContext()
    assert list(install_plugins(bundle, [_SearchPlugin], ctx)) == []


def test_install_plugins_iterates_multiple():
    class _OtherPlugin:
        NAME: ClassVar[StrId] = StrId("other")

        @classmethod
        def config(cls) -> ObjectSchema[_SearchCfg]:
            return _SEARCH_SCHEMA

        @classmethod
        def build(cls, cfg: _SearchCfg, ctx: ExtensionContext) -> _BuiltSearch:
            return _BuiltSearch(cfg=cfg, ctx=ctx)

    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("$tool.search.enable"): BoolValue(True),
                    _path("$tool.search.base_url"): StringValue("https://search"),
                    _path("$tool.other.enable"): BoolValue(True),
                    _path("$tool.other.base_url"): StringValue("https://other"),
                }
            )
        ]
    )
    ctx = ExtensionContext()
    artifacts = list(install_plugins(bundle, [_SearchPlugin, _OtherPlugin], ctx))
    assert len(artifacts) == 2
    urls = sorted(a.cfg.base_url for a in artifacts)
    assert urls == ["https://other", "https://search"]


# --- Protocol structural-check ---


def test_search_plugin_satisfies_runtime_protocol():
    """Plugin — runtime_checkable Protocol; isinstance проверяет членов."""
    assert isinstance(_SearchPlugin, type)
    # Сам класс (не инстанс) — у Plugin поля classmethod-уровня.
    assert hasattr(_SearchPlugin, "NAME")
    assert callable(getattr(_SearchPlugin, "config", None))
    assert callable(getattr(_SearchPlugin, "build", None))


# Re-export, чтобы pyright не считал импорт неиспользованным.
_ = ParseBool
_ = Plugin
