"""ConfigLoader: главный TOML-файл, наложения, подключённые файлы и окружение."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from omegaconf import OmegaConf
from omegaconf.errors import ReadonlyConfigError
from pydantic import BaseModel, ConfigDict

from boba.config import (
    ConfigError,
    ConfigFileSections,
    ConfigLoader,
    ConfigModel,
    ConfigSchema,
    bind,
)
from boba.config.paths import PathsConfig

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


class _FileApp(ConfigModel):
    CONFIG_FILES: ClassVar = (ConfigFileSections("plugins", "tool"),)
    plugins: str = ""


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
    overlay.write_text(
        '[postgres]\n    dbname = "stand_db"\n[tool.kb]\n    enable = false\n'
    )

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
    main.write_text(
        '[postgres]\n    password = "${oc.env:BOBA_TEST_ABSENT_PASSWORD}"\n'
    )

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


@pytest.fixture
def glob_config(tmp_path: Path) -> Path:
    (tmp_path / "plugins").mkdir()
    main = tmp_path / "config.toml"
    main.write_text('plugins = "plugins/*.conf"\n')

    return main


def test_glob_reads_only_matching_files_in_name_order(glob_config: Path) -> None:
    plugins = glob_config.parent / "plugins"
    (plugins / "z.conf").write_text(
        'enable = true\n[sandbox]\nprogram = "plugins/db"\n'
    )
    (plugins / "a.conf").write_text("enable = false\n")
    (plugins / "notes.toml").write_text("not valid TOML")

    raw = ConfigLoader([glob_config], schema=ConfigSchema(_FileApp)).load()

    assert list(raw.tool) == ["a", "z"]
    assert raw.tool.a.enable is False
    assert raw.tool.z.sandbox.program == "plugins/db"


def test_glob_preserves_plugin_settings_under_an_overlay(glob_config: Path) -> None:
    plugins = glob_config.parent / "plugins"
    (plugins / "doc.conf").write_text(
        'connection = "${postgres}"\n'
        '[ocr]\nprovider = "rapidocr"\nmodels_dir = "/var/cache/rapidocr"\n'
    )
    overlay = glob_config.parent / "debug.toml"
    overlay.write_text('[postgres]\nhost = "db.example"\n[tool.doc.ocr]\nthreads = 2\n')

    raw = ConfigLoader([glob_config, overlay], schema=ConfigSchema(_FileApp)).load()

    assert raw.tool.doc.connection.host == "db.example"
    assert raw.tool.doc.ocr.provider == "rapidocr"
    assert raw.tool.doc.ocr.models_dir == "/var/cache/rapidocr"
    assert raw.tool.doc.ocr.threads == 2


def test_glob_allows_an_empty_plugin_directory(glob_config: Path) -> None:
    raw = ConfigLoader([glob_config], schema=ConfigSchema(_FileApp)).load()

    assert list(raw.tool) == []


def test_glob_refuses_a_missing_directory(glob_config: Path) -> None:
    glob_config.write_text('plugins = "missing/*.conf"\n')

    with pytest.raises(ConfigError, match="missing"):
        ConfigLoader([glob_config], schema=ConfigSchema(_FileApp)).load()


def test_glob_reports_the_file_with_invalid_toml(glob_config: Path) -> None:
    (glob_config.parent / "plugins" / "broken.conf").write_text("enable = [")

    with pytest.raises(ConfigError, match=r"broken\.conf"):
        ConfigLoader([glob_config], schema=ConfigSchema(_FileApp)).load()


def test_glob_refuses_duplicate_section_names(glob_config: Path) -> None:
    glob_config.write_text('plugins = "plugins/*"\n')
    plugins = glob_config.parent / "plugins"
    (plugins / "doc.conf").write_text("enable = true\n")
    (plugins / "doc.toml").write_text("enable = false\n")

    with pytest.raises(ConfigError, match="multiple files name section 'doc'"):
        ConfigLoader([glob_config], schema=ConfigSchema(_FileApp)).load()


def test_file_sections_also_accept_a_single_path(glob_config: Path) -> None:
    glob_config.write_text('plugins = "plugins/doc.conf"\n')
    (glob_config.parent / "plugins" / "doc.conf").write_text("enable = true\n")

    raw = ConfigLoader([glob_config], schema=ConfigSchema(_FileApp)).load()

    assert list(raw.tool) == ["doc"]


def test_file_sections_work_without_an_application_model(glob_config: Path) -> None:
    (glob_config.parent / "plugins" / "doc.conf").write_text("enable = true\n")

    raw = ConfigLoader([glob_config], file_sections=_FileApp.CONFIG_FILES).load()

    assert raw.tool.doc.enable is True


def test_scalar_data_path_builds_the_instance_paths() -> None:
    raw = OmegaConf.create({"data": "/srv/boba/data"})

    paths = bind(raw, "data", PathsConfig)

    assert paths.data("workspace") == Path("/srv/boba/data/workspace")


def test_scalar_data_path_rejects_a_relative_directory() -> None:
    raw = OmegaConf.create({"data": "relative/data"})

    with pytest.raises(ConfigError, match="expected an absolute path"):
        bind(raw, "data", PathsConfig)


def test_broken_toml_names_the_file(tmp_path: Path) -> None:
    main = tmp_path / "config.toml"
    main.write_text("[app\n")

    with pytest.raises(ConfigError, match=r"config\.toml"):
        ConfigLoader([main]).load()
