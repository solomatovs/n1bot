"""Plugin Protocol + helpers (is_enabled, install_plugins, resolve_config_type)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

import pytest
from pydantic import Field

from boba.plugin import (
    ExtensionContext,
    MissingExtensionError,
    Plugin,
    install_plugins,
    is_enabled,
    resolve_config_type,
)
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict


class _SearchCfg(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__SEARCH__",
        boba_toml_section="tool.search",
    )

    enable: bool = False
    base_url: str = Field(default="", description="URL поиска.")
    limit: int = Field(default=10, description="Лимит.")


class _BuiltSearch:
    def __init__(self, cfg: _SearchCfg, ctx: ExtensionContext) -> None:
        self.cfg = cfg
        self.ctx = ctx


class _SearchPlugin(Plugin[_SearchCfg, _BuiltSearch]):
    NAME: ClassVar[str] = "search"

    @classmethod
    def build(
        cls,
        cfg: _SearchCfg,
        ctx: ExtensionContext,
    ) -> Iterable[_BuiltSearch]:
        yield _BuiltSearch(cfg=cfg, ctx=ctx)


# is_enabled


def test_is_enabled_reads_enable_attribute():
    cfg = _SearchCfg(enable=True)
    assert is_enabled(cfg) is True


def test_is_enabled_default_false_when_attribute_missing():
    class _NoEnable:
        pass

    assert is_enabled(_NoEnable()) is False


def test_is_enabled_false_when_attribute_false():
    cfg = _SearchCfg(enable=False)
    assert is_enabled(cfg) is False


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


def test_install_plugins_skips_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOBA_TOOL__SEARCH__ENABLE", raising=False)
    monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)
    ctx = ExtensionContext()
    artifacts = list(install_plugins([_SearchPlugin], ctx))
    assert artifacts == []


def test_install_plugins_materializes_and_builds_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BOBA_TOOL__SEARCH__ENABLE", "true")
    monkeypatch.setenv("BOBA_TOOL__SEARCH__BASE_URL", "https://example.com")
    monkeypatch.setenv("BOBA_TOOL__SEARCH__LIMIT", "50")
    monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)
    ctx = ExtensionContext()
    artifacts = list(install_plugins([_SearchPlugin], ctx))
    assert len(artifacts) == 1
    built = artifacts[0]
    assert isinstance(built, _BuiltSearch)
    assert built.cfg.base_url == "https://example.com"
    assert built.cfg.limit == 50
    assert built.ctx is ctx


def test_install_plugins_iterates_multiple(monkeypatch: pytest.MonkeyPatch):
    class _OtherCfg(BobaFlatSettings):
        model_config = BobaSettingsConfigDict(
            case_sensitive=False,
            extra="forbid",
            boba_env_prefix="BOBA_TOOL__OTHER__",
            boba_toml_section="tool.other",
        )

        enable: bool = False
        base_url: str = ""

    class _OtherPlugin(Plugin[_OtherCfg, _BuiltSearch]):
        NAME: ClassVar[str] = "other"

        @classmethod
        def build(
            cls,
            cfg: _OtherCfg,
            ctx: ExtensionContext,
        ) -> Iterable[_BuiltSearch]:
            yield _BuiltSearch(cfg=cfg, ctx=ctx)  # type: ignore[arg-type]

    monkeypatch.setenv("BOBA_TOOL__SEARCH__ENABLE", "true")
    monkeypatch.setenv("BOBA_TOOL__SEARCH__BASE_URL", "https://search")
    monkeypatch.setenv("BOBA_TOOL__OTHER__ENABLE", "true")
    monkeypatch.setenv("BOBA_TOOL__OTHER__BASE_URL", "https://other")
    monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)
    ctx = ExtensionContext()
    artifacts = list(install_plugins([_SearchPlugin, _OtherPlugin], ctx))
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
