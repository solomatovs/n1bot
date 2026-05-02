"""AppConfig + AppConfigFactory: реестр DTO секций приложения."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import pytest
from boba_next import (
    AppConfig,
    AppConfigFactory,
    ChainConverter,
    ConfigBundleFactory,
    ConfigPath,
    ConfigSection,
    ConfigSource,
    ConfigValue,
    Default,
    FieldSpec,
    IntValue,
    MinValue,
    ObjectSchema,
    ParseBool,
    ParseInt,
    ParseString,
    Required,
    SectionAlreadyRegisteredError,
    SectionMissingError,
    StringValue,
)
from boba_patterns import StrId


class _DictSource(ConfigSource):
    """Тестовый ConfigSource: статический словарь ConfigPath → ConfigValue."""

    def __init__(
        self,
        name: str,
        values: Mapping[ConfigPath, ConfigValue],
        priority: int = 100,
    ) -> None:
        self._name = name
        self._values = dict(values)
        self._priority = priority

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        return dict(self._values)


@dataclass(frozen=True)
class _AppCoreCfg:
    log_level: str
    ssl_verify: bool


class AppCoreSection(ConfigSection[_AppCoreCfg]):
    id: ClassVar[StrId] = StrId("app_core")
    namespace: ClassVar[tuple[str, ...]] = ("app",)
    schema: ClassVar[ObjectSchema[_AppCoreCfg]] = ObjectSchema(
        fields=[
            FieldSpec("log_level", ChainConverter(Default("INFO"), ParseString())),
            FieldSpec("ssl_verify", ChainConverter(Default(False), ParseBool())),
        ],
        factory=_AppCoreCfg,
    )


@dataclass(frozen=True)
class _AgentCfg:
    max_iterations: int


class AgentSection(ConfigSection[_AgentCfg]):
    id: ClassVar[StrId] = StrId("agent")
    namespace: ClassVar[tuple[str, ...]] = ("agent",)
    schema: ClassVar[ObjectSchema[_AgentCfg]] = ObjectSchema(
        fields=[
            FieldSpec(
                "max_iterations",
                ChainConverter(Default(20), ParseInt(), MinValue(1)),
            ),
        ],
        factory=_AgentCfg,
    )


def _make_factory(*sources: ConfigSource) -> AppConfigFactory:
    """Helper: новый AppConfigFactory с указанными источниками."""
    bundle_factory = ConfigBundleFactory()
    bundle_factory.attach_sources(sources)
    return AppConfigFactory(bundle_factory)


def test_factory_build_with_defaults_when_no_sources():
    factory = _make_factory()
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    app = factory.build()
    expected_app = _AppCoreCfg(log_level="INFO", ssl_verify=False)
    assert app.section(AppCoreSection) == expected_app
    assert app.section(AgentSection) == _AgentCfg(max_iterations=20)


def test_factory_reads_overrides_from_source():
    factory = _make_factory(
        _DictSource(
            "dict",
            {
                ConfigPath.parse("$app.log_level"): StringValue("DEBUG"),
                ConfigPath.parse("$agent.max_iterations"): IntValue(50),
            },
        ),
    )
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    app = factory.build()
    assert app.section(AppCoreSection).log_level == "DEBUG"
    assert app.section(AgentSection).max_iterations == 50


def test_section_missing_error():
    factory = _make_factory()
    factory.register(AppCoreSection())
    app = factory.build()
    with pytest.raises(SectionMissingError):
        app.section(AgentSection)


def test_register_twice_raises():
    factory = _make_factory()
    factory.register(AppCoreSection())
    with pytest.raises(SectionAlreadyRegisteredError):
        factory.register(AppCoreSection())


def test_build_is_pure_each_call_returns_fresh_object():
    factory = _make_factory()
    factory.register(AppCoreSection())
    a1 = factory.build()
    a2 = factory.build()
    assert a1 is not a2
    assert a1.section(AppCoreSection) == a2.section(AppCoreSection)


def test_register_after_build_takes_effect_on_next_build():
    factory = _make_factory()
    factory.register(AppCoreSection())
    a1 = factory.build()
    factory.register(AgentSection())
    a2 = factory.build()
    assert a1.has(AgentSection) is False
    assert a2.has(AgentSection) is True


def test_app_has_returns_membership():
    factory = _make_factory()
    factory.register(AppCoreSection())
    app = factory.build()
    assert app.has(AppCoreSection) is True
    assert app.has(AgentSection) is False


def test_required_field_missing_propagates_error():
    """Если поле required и отсутствует — build пробрасывает FieldPathMissingError."""
    from boba_next import FieldPathMissingError

    @dataclass(frozen=True)
    class _Cfg:
        token: str

    class _Section(ConfigSection[_Cfg]):
        id: ClassVar[StrId] = StrId("svc")
        namespace: ClassVar[tuple[str, ...]] = ("svc",)
        schema: ClassVar[ObjectSchema[_Cfg]] = ObjectSchema(
            fields=[FieldSpec("token", ChainConverter(Required(), ParseString()))],
            factory=_Cfg,
        )

    factory = _make_factory()
    factory.register(_Section())
    with pytest.raises(FieldPathMissingError):
        factory.build()


def test_section_prefix_matches_namespace():
    sec = AgentSection()
    assert sec.prefix() == ConfigPath.parse("$agent")


def test_app_config_constructor_directly():
    """AppConfig можно создать вручную — без AppConfigFactory."""
    cfg = _AgentCfg(max_iterations=42)
    app = AppConfig({StrId("agent"): cfg})
    assert app.section(AgentSection) is cfg
