"""Тесты перенесённых инструментов: pg, kb, confluence."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from boba.sandbox import SandboxToolConfig
from boba.tool.kb import (
    PostgresKnowledgeBaseConfig,
    build_kb_tools,
)
from boba.tool.kb.confluence import (
    ConfluenceToolsConfig,
    build_confluence_tools,
)
from boba.tool.pg import (
    SqlExecutorConfig,
    build_pg_tools,
)
from boba.tool.pg import executor as pg_executor
from boba.toolkit.result import ErrorResult, TableResult, ToolArtifact
from boba.transport.http import HttpProfile


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class _NoLauncher:
    """Исполнитель-заглушка: тесты проверяют обвязку, песочница им не нужна."""

    def call_text(self, command: str, stdin: str) -> Any:
        raise AssertionError("песочница не должна вызываться")

    def call_json(self, entry: Any, request: Any, schema: Any) -> Any:
        raise AssertionError("песочница не должна вызываться")


def _no_launcher(tool: str) -> Any:
    return _NoLauncher()


def pg_config() -> SqlExecutorConfig:
    return SqlExecutorConfig.model_validate(
        {
            "profiles": {"main": {"host": "h", "dbname": "d", "user": "u"}},
            "sandbox": _SANDBOX,
        }
    )


def kb_config() -> PostgresKnowledgeBaseConfig:
    return PostgresKnowledgeBaseConfig.model_validate(
        {
            "connection": {"host": "h", "dbname": "d", "user": "u"},
            "tables": {"pg_schema": "kb"},
            "embedding": {
                "model": "intfloat/multilingual-e5-small",
                "dim": 384,
                "batch_size": 8,
            },
            "sandbox": _SANDBOX,
        }
    )


def invoke(tool: Any, args: dict[str, Any]) -> Any:
    message = tool.invoke({"name": tool.name, "args": args, "id": "c1",
                           "type": "tool_call"})
    return ToolArtifact.revive(message.artifact)


class TestPgTools:
    def test_all_four_are_built(self) -> None:
        names = [t.name for t in build_pg_tools(pg_config(), _no_launcher)]
        assert names == ["list_targets", "list_tables", "describe_table", "query"]

    def test_list_targets_returns_whitelist(self) -> None:
        tool = build_pg_tools(pg_config(), _no_launcher)[0]
        result = invoke(tool, {})
        assert isinstance(result, TableResult)
        assert list(result.rows) == [{"target": "main"}]
        assert result.ok is True

    # query до сих пор принимает target, остальные уже connection_name
    _TARGET_ARG: ClassVar[dict[str, str]] = {
        "list_tables": "connection_name",
        "describe_table": "connection_name",
        "query": "target",
    }

    def test_unknown_target_becomes_error_result(self) -> None:
        """Профиль не в whitelist — ошибка инструмента, а не падение хода."""
        for name in ("list_tables", "describe_table", "query"):
            built = build_pg_tools(pg_config(), _no_launcher)
            tool = next(t for t in built if t.name == name)
            args = {self._TARGET_ARG[name]: "нет-такого"}
            if name == "describe_table":
                args["table"] = "t"
            if name == "query":
                args["sql"] = "select 1"
            result = invoke(tool, args)
            assert isinstance(result, ErrorResult), name
            assert result.error_kind == "unknown_target", name
            assert result.ok is False, name

    def test_sql_error_becomes_error_result(self, monkeypatch) -> None:
        def boom(*_args: Any, **_kwargs: Any):
            raise pg_executor.SqlQueryError("relation does not exist")

        monkeypatch.setattr(pg_executor.SqlExecutor, "execute", boom)
        built = build_pg_tools(pg_config(), _no_launcher)
        tool = next(t for t in built if t.name == "list_tables")
        result = invoke(tool, {"connection_name": "main"})
        assert isinstance(result, ErrorResult)
        assert result.ok is False
        assert "relation does not exist" in result.message


class TestKbTools:
    def test_all_four_are_built(self) -> None:
        names = [t.name for t in build_kb_tools(kb_config(), _no_launcher)]
        assert names == [
            "kb_vector_search",
            "kb_fts_search",
        ]

    def test_search_arguments(self) -> None:
        tool = build_kb_tools(kb_config(), _no_launcher)[0]
        assert set(tool.args) == {"query", "top_k", "snippet_chars"}


_SANDBOX = SandboxToolConfig.model_validate({
    "profile": {
        "rootfs": "",
        "ro_binds": (),
        "rw_binds": (),
        "rw_images": (),
        "image_template": "",
        "launcher": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "tmpfs": ("/tmp:64M",),  # noqa: S108
        "network": False,
        "env_set": {"PATH": "/usr/bin:/bin"},
        "timeout_sec": 30,
        "max_memory_bytes": 512 * 1024 * 1024,
        "max_cpu_sec": 30,
        "max_file_size_bytes": 64 * 1024 * 1024,
        "max_open_files": 1024,
        "max_processes": 256,
        "max_output_bytes": 4 * 1024 * 1024,
        "cgroup_base": "",
        "oom_score_adj": 0,
        "cwd": "/tmp",  # noqa: S108
    },
    "override": {},
})


class TestConfluenceTools:
    def test_all_four_are_built(self) -> None:
        cfg = ConfluenceToolsConfig(
            confluence=HttpProfile(base_url="https://confluence.example"),
        )
        names = [t.name for t in build_confluence_tools(cfg, _no_launcher)]
        assert names == [
            "confluence_fetch",
            "confluence_grep",
            "confluence_search",
            "confluence_spaces",
        ]

    def test_network_error_becomes_error_result(self) -> None:
        cfg = ConfluenceToolsConfig(
            confluence=HttpProfile(base_url="http://127.0.0.1:1"),
        )
        tool = next(
            t
            for t in build_confluence_tools(cfg, _no_launcher)
            if t.name == "confluence_fetch"
        )
        result = invoke(tool, {"page_id": "1"})
        assert isinstance(result, ErrorResult)
        assert result.ok is False
