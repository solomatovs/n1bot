"""Минимальные TOML, ссылки на defaults и приоритет наложений."""

from pathlib import Path
from typing import Annotated, Literal

import pytest
from omegaconf import OmegaConf
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, RootModel

from boba.config import ConfigError, ConfigLoader, ConfigSchema, bind


class _Process(BaseModel):
    command: str
    timeout: float = Field(default=30, gt=0)


class _Remote(BaseModel):
    endpoint: str
    auth: str
    timeout: float = Field(default=60, gt=0)


class _Agent(BaseModel):
    process: _Process | _Remote


class _Local(BaseModel):
    kind: Literal["local"]
    chunk_bytes: int = Field(
        default=65536,
        validation_alias=AliasChoices(
            "chunk_bytes", AliasPath("mounting", "copy_chunk_bytes")
        ),
    )


class _Image(BaseModel):
    kind: Literal["image"]
    mount_dir: str
    timeout: int = 60


class _Auth(BaseModel):
    type: Literal["local"]
    secret: str
    max_skew: int = 60


class _App(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db_schema: str = Field(alias="schema")
    agent: _Agent
    auth: list[_Auth]
    storage: Annotated[_Local | _Image, Field(discriminator="kind")]


CONFIG = """
copied_timeout = "${agent.process.timeout}"
copied_skew = "${auth.local.max_skew}"
copied_chunk = "${storage.chunk_bytes}"

[app]
schema = "stand"

[agent.process]
command = "boba"

[auth.local]
type = "local"
secret = "${oc.env:BOBA_TEST_CONFIG_SECRET}"

[storage]
kind = "local"
"""


@pytest.fixture
def loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigLoader:
    monkeypatch.setenv("BOBA_TEST_CONFIG_SECRET", "configured-secret")
    main = tmp_path / "config.toml"
    main.write_text(CONFIG, encoding="utf-8")
    return ConfigLoader([main], schema=ConfigSchema(_App, values=("auth",)))


def test_defaults_are_visible_to_references_and_app(loader: ConfigLoader) -> None:
    raw = loader.load()
    app = bind(raw, "app", _App)

    assert raw.copied_timeout == app.agent.process.timeout == 30
    assert raw.copied_skew == app.auth[0].max_skew == 60
    assert isinstance(app.storage, _Local)
    assert raw.copied_chunk == app.storage.chunk_bytes == 65536
    assert app.auth[0].secret == "configured-secret"
    assert "mount_dir" not in raw.storage


def test_explicit_app_override_wins_and_overlay_references_follow(
    loader: ConfigLoader,
) -> None:
    raw = loader.load_over(
        OmegaConf.create(
            {
                "agent": {"process": {"timeout": 90}},
                "app": {"agent": {"process": {"command": "other", "timeout": 120}}},
            }
        )
    )

    assert raw.copied_timeout == 90
    process = bind(raw, "app", _App).agent.process
    assert isinstance(process, _Process)
    assert process.timeout == 120
    assert process.command == "other"


def test_empty_auth_list_does_not_restore_top_level_auth(loader: ConfigLoader) -> None:
    raw = loader.load_over(OmegaConf.create({"app": {"auth": []}}))

    assert bind(raw, "app", _App).auth == []


def test_invalid_override_is_still_refused(loader: ConfigLoader) -> None:
    raw = loader.load_over(OmegaConf.create({"agent": {"process": {"timeout": 0}}}))

    with pytest.raises(ConfigError, match="timeout"):
        bind(raw, "app", _App)


def test_untagged_variant_uses_its_own_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOBA_TEST_CONFIG_SECRET", "configured-secret")
    main = tmp_path / "config.toml"
    main.write_text(
        CONFIG.replace(
            'command = "boba"',
            'endpoint = "https://agent.invalid"\nauth = "configured"',
        )
    )
    raw = ConfigLoader([main], schema=ConfigSchema(_App, values=("auth",))).load()

    assert raw.copied_timeout == 60
    assert isinstance(bind(raw, "app", _App).agent.process, _Remote)


def test_alias_path_is_not_masked_by_a_default(loader: ConfigLoader) -> None:
    raw = loader.load_over(
        OmegaConf.create(
            {
                "storage": {"mounting": {"copy_chunk_bytes": 4096}},
                "copied_chunk": "unused",
            }
        )
    )

    storage = bind(raw, "app", _App).storage
    assert isinstance(storage, _Local)
    assert storage.chunk_bytes == 4096


def test_defaults_of_a_referenced_table_are_added(loader: ConfigLoader) -> None:
    raw = loader.load_over(
        OmegaConf.create(
            {
                "agent": {"process": "${command}"},
                "command": {"command": "external"},
            }
        )
    )

    assert bind(raw, "app", _App).agent.process.timeout == 30
    assert raw.command.timeout == 30


def test_selected_variant_keeps_its_required_fields(loader: ConfigLoader) -> None:
    raw = loader.load_over(
        OmegaConf.create(
            {
                "storage": {"kind": "image"},
                "copied_chunk": "unused",
            }
        )
    )

    assert raw.storage.timeout == 60
    assert "chunk_bytes" not in raw.storage
    with pytest.raises(ConfigError, match="mount_dir"):
        bind(raw, "app", _App)


def test_included_section_keeps_its_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOBA_TEST_CONFIG_SECRET", "configured-secret")
    (tmp_path / "agent.toml").write_text('[process]\ncommand = "included"\n')
    main = tmp_path / "config.toml"
    body = CONFIG.replace('[agent.process]\ncommand = "boba"', "")
    main.write_text('agent = "${toml:agent.toml}"\n' + body)
    loader = ConfigLoader([main], schema=ConfigSchema(_App, values=("auth",)))
    raw = loader.load()

    assert raw.copied_timeout == 30
    process = bind(raw, "app", _App).agent.process
    assert isinstance(process, _Process)
    assert process.command == "included"
    (tmp_path / "agent.toml").write_text(
        '[process]\ncommand = "changed"\ntimeout = 45\n'
    )
    reloaded = loader.load()

    assert raw.copied_timeout == 30
    assert reloaded.copied_timeout == 45


def test_absent_optional_section_is_available_to_references(tmp_path: Path) -> None:
    class _Defaults(BaseModel):
        timeout: int = 10

    class _OptionalApp(BaseModel):
        service: _Defaults = Field(default_factory=_Defaults)

    main = tmp_path / "config.toml"
    main.write_text('copied = "${service.timeout}"\n')
    raw = ConfigLoader([main], schema=ConfigSchema(_OptionalApp)).load()

    assert raw.copied == 10
    assert bind(raw, "app", _OptionalApp).service.timeout == 10


def test_defaults_inside_root_model_collections(tmp_path: Path) -> None:
    class _Collections(BaseModel):
        named: RootModel[dict[str, _Process]]
        ordered: RootModel[list[_Process]]

    main = tmp_path / "config.toml"
    main.write_text("""
copied = "${named.worker.timeout}"
[named.worker]
command = "named"
[[ordered]]
command = "ordered"
""")
    raw = ConfigLoader([main], schema=ConfigSchema(_Collections)).load()
    app = bind(raw, "app", _Collections)

    assert raw.copied == app.named.root["worker"].timeout == 30
    assert app.ordered.root[0].timeout == 30
