"""ConfigBuilder: явная сборка слоёв-источников в инстанс (не глобал)."""

from __future__ import annotations

from pathlib import Path

from omegaconf import OmegaConf

from boba.settings import ConfigBuilder


def test_yaml_layer(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("a:\n  host: h\n  port: 6000\n")
    cfg = ConfigBuilder().add_yaml(f).build()
    assert cfg.a.host == "h"
    assert cfg.a.port == 6000


def test_missing_yaml_is_noop(tmp_path: Path) -> None:
    cfg = ConfigBuilder().add_yaml(tmp_path / "nope.yaml").add_dict({"x": 1}).build()
    assert cfg.x == 1


def test_layer_priority_later_wins(tmp_path: Path) -> None:
    base = tmp_path / "config.yaml"
    base.write_text("a:\n  port: 1\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("a:\n  port: 2\n")
    cfg = (
        ConfigBuilder()
        .add_yaml(base)
        .add_yaml(secrets)
        .add_cli(["a.port=3"])
        .build()
    )
    assert cfg.a.port == 3


def test_secrets_merge_into_referenced_block(tmp_path: Path) -> None:
    base = tmp_path / "config.yaml"
    base.write_text("openai:\n  openrouter:\n    base_url: u\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("openai:\n  openrouter:\n    api_key: SECRET\n")
    cfg = ConfigBuilder().add_yaml(base).add_yaml(secrets).build()
    assert cfg.openai.openrouter.base_url == "u"
    assert cfg.openai.openrouter.api_key == "SECRET"


def test_empty_builder() -> None:
    assert OmegaConf.to_container(ConfigBuilder().build()) == {}
