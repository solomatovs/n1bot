"""ConfigLoader: главный TOML-файл, наложения, подключённые файлы и окружение."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import OmegaConf
from omegaconf.errors import ReadonlyConfigError
from pydantic import BaseModel, ConfigDict

from boba.config import ConfigError, ConfigLoader, bind

MAIN = """
tool = "${toml_dir:plugins}"

[app]
    schema = "mcp"

[postgres]
    host   = "db.example"
    dbname = "aid"

[mcp.endpoints.all]
    prompts = "${toml_dir:prompts/all}"
"""

PLUGIN = """
enable     = true
connection = "${postgres}"
db_schema  = "${app.schema}"
"""

PROMPT = """
description = "Review a query"
text        = "run the query as is"
"""


class _Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    dbname: str


class _Plugin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enable: bool
    connection: _Connection
    db_schema: str


class _Prompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    text: str


class _Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompts: dict[str, _Prompt]


def _instance(root: Path) -> Path:
    """Конфиг экземпляра: главный файл, плагины и промпты рядом с ним."""
    (root / "plugins").mkdir(parents=True)
    (root / "plugins" / "kb.toml").write_text(PLUGIN, encoding="utf-8")
    (root / "prompts" / "all").mkdir(parents=True)
    (root / "prompts" / "all" / "sql_review.toml").write_text(PROMPT, encoding="utf-8")
    main = root / "config.toml"
    main.write_text(MAIN, encoding="utf-8")

    return main


def test_plugin_file_sees_the_sections_of_the_config(tmp_path: Path) -> None:
    raw = ConfigLoader([_instance(tmp_path)]).load()
    plugin = bind(raw, "tool.kb", _Plugin)

    assert plugin.connection.host == "db.example"
    assert plugin.db_schema == "mcp"


def test_prompt_directory_becomes_a_table_of_files(tmp_path: Path) -> None:
    raw = ConfigLoader([_instance(tmp_path)]).load()
    endpoint = bind(raw, "mcp.endpoints.all", _Endpoint)

    assert endpoint.prompts["sql_review"].description == "Review a query"


def test_overlay_changes_only_its_keys_and_references_follow(tmp_path: Path) -> None:
    main = _instance(tmp_path)
    overlay = tmp_path / "stand.toml"
    overlay.write_text('[postgres]\n    dbname = "stand_db"\n[tool.kb]\n    enable = false\n')

    raw = ConfigLoader([main, overlay]).load()
    plugin = bind(raw, "tool.kb", _Plugin)

    assert plugin.connection.dbname == "stand_db"
    assert plugin.connection.host == "db.example"
    assert plugin.enable is False


def test_overlay_in_memory_is_merged_before_resolution(tmp_path: Path) -> None:
    """Наложение стенда в памяти меняет ключ, и ссылки на него следуют."""
    loader = ConfigLoader([_instance(tmp_path)])
    raw = loader.load_over(OmegaConf.create({"postgres": {"dbname": "stand_db"}}))

    assert bind(raw, "tool.kb", _Plugin).connection.dbname == "stand_db"


def test_files_are_read_once_at_load(tmp_path: Path) -> None:
    """Второй конфиг из другого каталога в том же процессе не меняет первый."""
    first = ConfigLoader([_instance(tmp_path / "first")]).load()
    second_root = tmp_path / "second"
    _instance(second_root)
    (second_root / "plugins" / "kb.toml").write_text(PLUGIN.replace("true", "false"))
    ConfigLoader([second_root / "config.toml"]).load()

    assert bind(first, "tool.kb", _Plugin).enable is True


def test_loaded_tree_is_read_only(tmp_path: Path) -> None:
    raw = ConfigLoader([_instance(tmp_path)]).load()

    with pytest.raises(ReadonlyConfigError):
        OmegaConf.update(raw, "postgres.dbname", "other")


def test_missing_environment_variable_fails_the_load(tmp_path: Path) -> None:
    main = tmp_path / "config.toml"
    main.write_text('[postgres]\n    password = "${oc.env:BOBA_TEST_ABSENT_PASSWORD}"\n')

    with pytest.raises(ConfigError, match="BOBA_TEST_ABSENT_PASSWORD"):
        ConfigLoader([main]).load()


def test_dangling_reference_fails_the_load(tmp_path: Path) -> None:
    main = tmp_path / "config.toml"
    main.write_text('[app]\n    postgres = "${postgres}"\n')

    with pytest.raises(ConfigError, match=r"app\.postgres"):
        ConfigLoader([main]).load()


def test_missing_plugin_directory_fails_the_load(tmp_path: Path) -> None:
    main = tmp_path / "config.toml"
    main.write_text('tool = "${toml_dir:plugins}"\n')

    with pytest.raises(ConfigError, match="plugins"):
        ConfigLoader([main]).load()


def test_broken_toml_names_the_file(tmp_path: Path) -> None:
    main = tmp_path / "config.toml"
    main.write_text("[app\n")

    with pytest.raises(ConfigError, match="config.toml"):
        ConfigLoader([main]).load()
