"""EnvSource: BOBA_<SEG>__<SEG>__<INDEX> → ConfigPath."""

from __future__ import annotations

from pathlib import Path

from boba.config.env.next import EnvFileSource, EnvSource
from boba_next import ConfigPath, StringValue


def test_simple_two_segments():
    snap = EnvSource({"BOBA_AGENT__MAX_ITERATIONS": "200"}).load()
    assert snap == {ConfigPath.parse("$agent.max_iterations"): StringValue("200")}


def test_nested_extension_path():
    snap = EnvSource(
        {
            "BOBA_EXT__CHROMADB__TOOLS__KB_SEARCH__ENABLED": "true",
        }
    ).load()
    assert snap == {
        ConfigPath.parse("$ext.chromadb.tools.kb_search.enabled"): StringValue("true"),
    }


def test_index_segment_from_digits():
    snap = EnvSource({"BOBA_MODELS__0": "qwen3"}).load()
    assert snap == {ConfigPath.parse("$models[0]"): StringValue("qwen3")}


def test_underscores_inside_segment_preserved():
    snap = EnvSource({"BOBA_AGENT__MAX_ITERATIONS": "200"}).load()
    # Внутри сегмента подчёркивание сохраняется (max_iterations).
    assert ConfigPath.parse("$agent.max_iterations") in snap


def test_non_boba_keys_ignored():
    snap = EnvSource({"PATH": "/usr", "BOBA_X": "y"}).load()
    assert ConfigPath.parse("$x") in snap
    assert all("path" not in p.render() for p in snap)


def test_env_file_source_reads_secret(tmp_path: Path):
    secret = tmp_path / "secret"
    secret.write_text("token-123\n", encoding="utf-8")
    snap = EnvFileSource({"BOBA_LLM__API_KEY_FILE": str(secret)}).load()
    assert snap == {
        ConfigPath.parse("$llm.api_key"): StringValue("token-123"),
    }


def test_env_file_source_skips_missing_file():
    snap = EnvFileSource({"BOBA_X_FILE": "/no/such/file"}).load()
    assert snap == {}


def test_env_priority_higher_than_toml_default():
    assert EnvSource({}).priority() > 100  # TomlSource default
