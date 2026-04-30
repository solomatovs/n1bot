"""CliSource: --dotted.path=value → ConfigPath."""

from __future__ import annotations

from boba.config.cli.next import CliSource, parse_argv_path
from boba.domain.core.confignext import ConfigPath, StringValue


def test_equals_form():
    snap = CliSource(["--ext.chromadb.enabled=true"]).load()
    assert snap == {
        ConfigPath.parse("$ext.chromadb.enabled"): StringValue("true"),
    }


def test_space_form():
    snap = CliSource(["--ext.chromadb.enabled", "false"]).load()
    assert snap == {
        ConfigPath.parse("$ext.chromadb.enabled"): StringValue("false"),
    }


def test_bare_flag_becomes_true():
    snap = CliSource(["--ext.html.enabled"]).load()
    assert snap == {
        ConfigPath.parse("$ext.html.enabled"): StringValue("true"),
    }


def test_dash_in_segment_normalized_to_underscore():
    snap = CliSource(["--agent.max-iterations=200"]).load()
    assert snap == {
        ConfigPath.parse("$agent.max_iterations"): StringValue("200"),
    }


def test_index_segment_from_brackets():
    snap = CliSource(["--models[0]=qwen3"]).load()
    assert snap == {ConfigPath.parse("$models[0]"): StringValue("qwen3")}


def test_index_segment_from_dot_digits():
    snap = CliSource(["--models.0=qwen3"]).load()
    assert snap == {ConfigPath.parse("$models[0]"): StringValue("qwen3")}


def test_non_flag_args_skipped():
    snap = CliSource(["positional", "--a=1", "another"]).load()
    assert snap == {ConfigPath.parse("$a"): StringValue("1")}


def test_two_flags_in_a_row_first_is_bare():
    snap = CliSource(["--first", "--second=x"]).load()
    assert snap == {
        ConfigPath.parse("$first"): StringValue("true"),
        ConfigPath.parse("$second"): StringValue("x"),
    }


def test_parse_argv_path_handles_mixed_segments():
    p = parse_argv_path("ext.chromadb.tools.kb_search.params.top_k")
    assert p is not None
    assert p.render() == "$ext.chromadb.tools.kb_search.params.top_k"


def test_parse_argv_path_invalid_returns_none():
    assert parse_argv_path("") is None
    assert parse_argv_path("[0") is None  # незакрытая скобка
    assert parse_argv_path("1bad") is None  # начинается с цифры — невалидное имя
    assert parse_argv_path(".") is None


def test_parse_argv_path_pure_digits_become_index():
    p = parse_argv_path("42")
    assert p is not None
    assert p.render() == "$[42]"


def test_cli_priority_higher_than_env_default():
    assert CliSource([]).priority() > 300  # EnvSource default
