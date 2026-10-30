"""Манифест плагина v1: объявление автора становится данными для сервера."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    Secret,
    SecretStr,
    ValidationError,
)

from boba.stand_core.fake_toolmod import PLUGIN, fake_stream
from boba.toolkit.facade import (
    Injected,
    NotLogged,
    UserConnection,
    WarmupHook,
    tool,
    warmup,
)
from boba.toolkit.manifest import (
    ConnectionProfile,
    ContextValue,
    InjectedRole,
    ManifestBuilder,
    ManifestError,
    PluginManifest,
    PluginSetup,
    PluginStorage,
    ToolPlugin,
    WarmupSection,
)
from boba.toolkit.ports import ToolProgress
from boba.toolkit.result import MarkdownResult

BUILDER = ManifestBuilder()


class ProbeProfile(ConnectionProfile):
    kind: Literal["probe"] = "probe"
    host: str


class ProbeCaller(ContextValue):
    ROLE = InjectedRole.SUBJECT

    login: str


class ProbeConfig(BaseModel):
    limit: int = Field(gt=0)
    connection: ProbeProfile | None = None


class ProbeStorage(PluginStorage):
    def __init__(self, cfg: ProbeConfig) -> None:
        self._cfg = cfg

    async def setup(self) -> None:
        return


@tool
async def probe_tool(  # noqa: PLR0913 — у пробы параметры всех видов
    sql: Annotated[str, Field(min_length=1, description="Запрос")],
    password: Annotated[str, NotLogged, Field(description="Пароль")],
    target: Annotated[ProbeProfile, UserConnection],
    cfg: Annotated[ProbeConfig, Injected],
    caller: Annotated[ProbeCaller, Injected],
    progress: Annotated[ToolProgress, Injected],
) -> MarkdownResult:
    """Пробный инструмент со всеми видами параметров."""
    return MarkdownResult(text=f"{sql}|{target.host}|{cfg.limit}|{caller.login}")


class ProbeWarmupConfig(BaseModel):
    cache: str = Field(
        default="probe", validation_alias=AliasChoices("cache_dir", "cache")
    )
    depth: int = Field(default=1, validation_alias=AliasPath("tuning", "depth"))


@warmup
async def warm_probe(cfg: ProbeWarmupConfig) -> None:
    """Прогрев пробного плагина."""


@warmup
async def warm_with_profile(cfg: ProbeConfig) -> None:
    """Прогрев, которому достался бы профиль соединения."""


class ProbeVault(BaseModel):
    token: SecretStr


class SecretWarmupConfig(BaseModel):
    vault: ProbeVault


@warmup
async def warm_with_secret(cfg: SecretWarmupConfig) -> None:
    """Прогрев, которому достался бы токен."""


class GenericSecretConfig(BaseModel):
    pins: dict[str, Secret[int]]


@warmup
async def warm_with_generic_secret(cfg: GenericSecretConfig) -> None:
    """Прогрев с секретом внутри словаря."""


class ListedWarmupConfig(BaseModel):
    targets: list[ProbeProfile]


@warmup
async def warm_with_listed(cfg: ListedWarmupConfig) -> None:
    """Прогрев с профилями соединений списком."""


class ListedConfig(BaseModel):
    targets: list[ProbeProfile]


@tool
async def listed(cfg: Annotated[ListedConfig, Injected]) -> MarkdownResult:
    """Профили соединений списком в конфиге."""
    return MarkdownResult(text="")


@tool
async def scalar(token: Annotated[SecretStr, Injected]) -> MarkdownResult:
    """Injected-параметр не моделью."""
    return MarkdownResult(text="")


PROBE = ToolPlugin(
    id="probe",
    tools=(probe_tool,),
    setup=PluginSetup(ProbeConfig, ProbeStorage),
    warmups=(warm_probe,),
    network=True,
)


class TestToolDecl:
    def test_service_parameters_leave_the_model_schema(self) -> None:
        decl = BUILDER.tool(probe_tool)

        assert list(decl.properties()) == ["sql", "password"]
        assert decl.input_schema["required"] == ["sql", "password"]
        assert decl.connections == {"target": "probe"}
        assert decl.injected == {
            "cfg": InjectedRole.CONFIG,
            "caller": InjectedRole.SUBJECT,
            "progress": InjectedRole.PROGRESS,
        }

    def test_not_logged_argument_is_marked(self) -> None:
        properties = BUILDER.tool(probe_tool).properties()

        assert properties["password"]["x-boba-not-logged"] is True
        assert "x-boba-not-logged" not in properties["sql"]

    def test_ports_become_the_stream_declaration(self) -> None:
        decl = BUILDER.tool(fake_stream)

        assert {port.name for port in decl.stream.ports} == {"feed", "out"}
        assert "feed" not in decl.properties()


class TestPluginManifest:
    def test_manifest_carries_the_declaration(self) -> None:
        manifest = BUILDER.manifest(PROBE, "1.2.3")

        assert manifest.spec == PluginManifest.SPEC
        assert manifest.setup
        assert manifest.network
        assert [(w.module, w.hook) for w in manifest.warmups] == [
            (__name__, "warm_probe")
        ]
        assert manifest.warmups[0].keys == ("cache_dir", "cache", "tuning")
        assert [(c.pointer, c.kind) for c in manifest.connections_in_config] == [
            ("/connection", "probe")
        ]

    def test_manifest_survives_its_json(self) -> None:
        manifest = BUILDER.manifest(PROBE, "1.2.3")

        again = PluginManifest.model_validate_json(manifest.model_dump_json())

        assert again == manifest
        assert again.digest() == manifest.digest()

    def test_other_spec_is_refused_with_both_versions(self) -> None:
        raw = BUILDER.manifest(PLUGIN, "0").model_dump(mode="json")
        raw["spec"] = 2

        with pytest.raises(ValidationError, match=r"spec 2 .*spec 1"):
            PluginManifest.model_validate(raw)

    def test_unknown_field_is_refused(self) -> None:
        raw = BUILDER.manifest(PLUGIN, "0").model_dump(mode="json")
        raw["extra"] = True

        with pytest.raises(ValidationError, match="extra"):
            PluginManifest.model_validate(raw)

    def test_section_is_checked_by_the_plugin_models(self) -> None:
        problems = list(BUILDER.unfit(PROBE, {"limit": 0}))

        assert len(problems) == 1
        assert "[tool.probe] does not fit ProbeConfig" in problems[0]

    def test_profile_inside_a_list_is_refused(self) -> None:
        plugin = ToolPlugin(id="listed", tools=(listed,))

        with pytest.raises(ManifestError, match="/targets"):
            BUILDER.manifest(plugin, "0")

    def test_injected_scalar_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="token"):
            BUILDER.tool(scalar)


class TestWarmupSection:
    def test_zygote_gets_only_the_warmup_keys(self) -> None:
        manifest = BUILDER.manifest(PROBE, "0")
        section = {
            "limit": 1,
            "connection": {"kind": "probe", "host": "db"},
            "cache_dir": "probe-cache",
            "tuning": {"depth": 2},
        }

        picked = WarmupSection().of(manifest, section)

        assert picked == {"cache_dir": "probe-cache", "tuning": {"depth": 2}}

    def test_plugin_without_warmups_gets_an_empty_section(self) -> None:
        manifest = BUILDER.manifest(ToolPlugin(id="bare", tools=(probe_tool,)), "0")

        assert WarmupSection().of(manifest, {"limit": 1, "cache": "x"}) == {}

    @pytest.mark.parametrize(
        ("hook", "pointer", "what"),
        [
            (warm_with_profile, "/connection", "connection profile ProbeProfile"),
            (warm_with_secret, "/vault/token", "secret SecretStr"),
            (warm_with_generic_secret, "/pins", "secret Secret"),
            (warm_with_listed, "/targets", "connection profile ProbeProfile"),
        ],
    )
    def test_secret_in_a_warmup_config_is_refused(
        self, hook: WarmupHook, pointer: str, what: str
    ) -> None:
        plugin = ToolPlugin(id="leaky", tools=(), warmups=(hook,))

        with pytest.raises(ManifestError) as caught:
            BUILDER.manifest(plugin, "0")

        text = str(caught.value)
        assert f"plugin 'leaky': warmup {__name__}.{hook.name}" in text
        assert f"holds a {what} at {pointer}" in text
        assert "long-lived zygote" in text
