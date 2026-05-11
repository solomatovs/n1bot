"""Plugin Protocol + helpers (mount_path_for, is_enabled, install_plugins)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath
from boba.config.source.dict import DictSource
from boba.patterns import StrId
from boba.plugin import (
    ExtensionContext,
    Plugin,
    config_path,
    install_plugins,
    is_enabled,
)
from boba.schema.coercion import (
    ChainCoercer,
    Default,
    ParseBool,
    ParseInt,
    ParseString,
    Required,
)
from boba.schema.declaration import FieldSpec, ObjectSchema
from boba.schema.value import BoolValue, IntValue, StringValue


@dataclass(frozen=True)
class _SearchCfg:
    base_url: str
    limit: int


_SEARCH_SCHEMA: ObjectSchema[_SearchCfg] = ObjectSchema(
    fields=[
        FieldSpec("base_url", ChainCoercer(Required(), ParseString()), ),
        FieldSpec("limit", ChainCoercer(Default(10), ParseInt())),
    ],
    factory=_SearchCfg,
)


@dataclass(frozen=True)
class _BuiltSearch:
    cfg: _SearchCfg
    ctx: ExtensionContext


class _SearchPlugin(Plugin[_SearchCfg, _BuiltSearch]):
    NAME: ClassVar[StrId] = StrId("search")

    @classmethod
    def config(cls) -> ObjectSchema[_SearchCfg]:
        return _SEARCH_SCHEMA

    @classmethod
    def build(
        cls,
        cfg: _SearchCfg,
        ctx: ExtensionContext,
    ) -> Iterable[_BuiltSearch]:
        yield _BuiltSearch(cfg=cfg, ctx=ctx)


def test_mount_path_for_uses_tool_prefix():
    assert config_path(StrId("search")) == ConfigPath.parse("tool.search")
    assert config_path(StrId("confluence_page")) == ConfigPath.parse("tool.confluence_page")


def test_is_enabled_default_false_when_absent():
    bundle = ConfigBundle.from_sources([DictSource({})])
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is False


def test_is_enabled_true_when_explicit_true():
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): BoolValue(True)})]
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is True


def test_is_enabled_false_when_explicit_false():
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): BoolValue(False)})]
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is False


def test_is_enabled_parses_string_true():
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): StringValue("true")})]
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is True


def test_is_enabled_garbage_string_treated_as_false():
    bundle = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("tool.search.enable"): StringValue("not-a-bool")})]
    )
    assert is_enabled(bundle, ConfigPath.parse("tool.search")) is False


# --- install_plugins ---


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
                    ConfigPath.parse("tool.search.base_url"): StringValue("https://example.com"),
                    ConfigPath.parse("tool.search.limit"): IntValue(50),
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
        [DictSource({ConfigPath.parse("tool.search.enable"): BoolValue(False)})]
    )
    ctx = ExtensionContext()
    assert list(install_plugins(bundle, [_SearchPlugin], ctx)) == []


def test_install_plugins_iterates_multiple():
    class _OtherPlugin(Plugin[_SearchCfg, _BuiltSearch]):
        NAME: ClassVar[StrId] = StrId("other")

        @classmethod
        def config(cls) -> ObjectSchema[_SearchCfg]:
            return _SEARCH_SCHEMA

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
                    ConfigPath.parse("tool.search.base_url"): StringValue("https://search"),
                    ConfigPath.parse("tool.other.enable"): BoolValue(True),
                    ConfigPath.parse("tool.other.base_url"): StringValue("https://other"),
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
