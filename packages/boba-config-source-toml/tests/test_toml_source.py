"""TomlSource: TOML-дерево → плоский snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.config.path import ConfigPath
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.value import BoolValue, IntValue, StringValue


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_scalars(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        [agent]
        max_iterations = 200
        enabled = true
        name = "boba"
        """,
    )
    snap = TomlSource(cfg).load()
    assert snap[ConfigPath.parse("$agent.max_iterations")] == IntValue(200)
    assert snap[ConfigPath.parse("$agent.enabled")] == BoolValue(True)
    assert snap[ConfigPath.parse("$agent.name")] == StringValue("boba")


def test_nested_subsections(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        [ext.chromadb]
        enabled = true

        [ext.chromadb.tools.kb_search]
        enabled = true
        description = "Поиск"

        [ext.chromadb.tools.kb_search.params.top_k]
        description = "Сколько"
        """,
    )
    snap = TomlSource(cfg).load()
    assert snap[ConfigPath.parse("$ext.chromadb.enabled")] == BoolValue(True)
    assert snap[
        ConfigPath.parse("$ext.chromadb.tools.kb_search.description")
    ] == StringValue("Поиск")
    assert snap[
        ConfigPath.parse("$ext.chromadb.tools.kb_search.params.top_k.description")
    ] == StringValue("Сколько")


def test_arrays_as_indexed_paths(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        [chainlit]
        models = ["qwen3", "gemini", "deepseek"]
        """,
    )
    snap = TomlSource(cfg).load()
    assert snap[ConfigPath.parse("$chainlit.models[0]")] == StringValue("qwen3")
    assert snap[ConfigPath.parse("$chainlit.models[1]")] == StringValue("gemini")
    assert snap[ConfigPath.parse("$chainlit.models[2]")] == StringValue("deepseek")


def test_array_of_tables(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        [[chainlit.models]]
        name = "qwen3"

        [[chainlit.models]]
        name = "gemini"
        """,
    )
    snap = TomlSource(cfg).load()
    assert snap[ConfigPath.parse("$chainlit.models[0].name")] == StringValue("qwen3")
    assert snap[ConfigPath.parse("$chainlit.models[1].name")] == StringValue("gemini")


def test_empty_when_file_missing(tmp_path: Path):
    snap = TomlSource(tmp_path / "absent.toml").load()
    assert snap == {}


def test_toml_file_source_reads_secret(tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("super-secret-value\n", encoding="utf-8")
    cfg = _write(
        tmp_path,
        f"""
        [llm]
        api_key_file = "{secret}"
        """,
    )
    snap = TomlFileSource(cfg).load()
    assert snap[ConfigPath.parse("$llm.api_key")] == StringValue("super-secret-value")


def test_toml_source_priority_default():
    src = TomlSource(None)
    assert src.priority() == 100
    assert src.name() == "toml"


def test_toml_file_source_higher_priority_than_main():
    main = TomlSource(None)
    secrets = TomlFileSource(None)
    assert secrets.priority() > main.priority()


@pytest.mark.parametrize(
    ("section", "expected_value"),
    [
        ("[a.b.c]\nleaf = 1", (ConfigPath.parse("$a.b.c.leaf"), IntValue(1))),
        ("[a]\n[a.b]\nleaf = 2", (ConfigPath.parse("$a.b.leaf"), IntValue(2))),
    ],
)
def test_table_paths_normalize(tmp_path: Path, section: str, expected_value):
    cfg = _write(tmp_path, section)
    snap = TomlSource(cfg).load()
    path, value = expected_value
    assert snap[path] == value
