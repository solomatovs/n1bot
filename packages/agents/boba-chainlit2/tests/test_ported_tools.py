"""Тесты перенесённых инструментов: pg, kb, confluence."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from boba.chainlit2.agent.tools.confluence import (
    ConfluenceToolsConfig,
    build_confluence_tools,
)
from boba.chainlit2.agent.tools.kb import (
    PostgresKnowledgeBaseConfig,
    build_kb_tools,
)
from boba.chainlit2.agent.tools.pg import (
    SqlExecutorConfig,
    build_pg_tools,
)
from boba.chainlit2.agent.tools.pg import executor as pg_executor
from boba.chainlit2.rendering.artifact import ToolArtifact
from boba.chainlit2.rendering.tool_result import ErrorResult, TableResult
from boba.transport.http import HttpProfile


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def pg_config() -> SqlExecutorConfig:
    return SqlExecutorConfig.model_validate(
        {"profiles": {"main": {"host": "h", "dbname": "d", "user": "u"}}}
    )


def kb_config() -> PostgresKnowledgeBaseConfig:
    return PostgresKnowledgeBaseConfig.model_validate(
        {
            "connection": {"host": "h", "dbname": "d", "user": "u"},
            "tables": {"pg_schema": "kb"},
            "embedding": {"model": "intfloat/multilingual-e5-small"},
        }
    )


def invoke(tool: Any, args: dict[str, Any]) -> Any:
    message = tool.invoke({"name": tool.name, "args": args, "id": "c1",
                           "type": "tool_call"})
    return ToolArtifact.revive(message.artifact)


class TestIsolation:
    def test_ported_tools_do_not_pull_v1_framework(self) -> None:
        """Ядро скопировано ради независимости — dishka тянуться не должен.

        Проверяем в отдельном процессе: в текущем boba.tools уже подтянут
        конфиг-загрузчиком из conftest.
        """
        probe = (
            "import sys;"
            "import boba.chainlit2.agent.tools.pg,"
            " boba.chainlit2.agent.tools.kb,"
            " boba.chainlit2.agent.tools.confluence;"
            "bad=[m for m in sys.modules"
            " if m.split('.')[0]=='dishka' or m.startswith('boba.tools')];"
            "print(bad)"
        )
        out = subprocess.run(  # noqa: S603 — фиксированный probe, не ввод
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.strip() == "[]"


class TestPgTools:
    def test_all_four_are_built(self) -> None:
        names = [t.name for t in build_pg_tools(pg_config())]
        assert names == ["list_targets", "list_tables", "describe_table", "query"]

    def test_list_targets_returns_whitelist(self) -> None:
        tool = build_pg_tools(pg_config())[0]
        result = invoke(tool, {})
        assert isinstance(result, TableResult)
        assert list(result.rows) == [{"target": "main"}]
        assert result.ok is True

    def test_unknown_target_becomes_error_result(self) -> None:
        """Профиль не в whitelist — ошибка инструмента, а не падение хода."""
        for name in ("list_tables", "describe_table", "query"):
            tool = next(t for t in build_pg_tools(pg_config()) if t.name == name)
            args = {"target": "нет-такого"}
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
        tool = next(t for t in build_pg_tools(pg_config()) if t.name == "list_tables")
        result = invoke(tool, {"target": "main"})
        assert isinstance(result, ErrorResult)
        assert result.ok is False
        assert "relation does not exist" in result.message


class TestKbTools:
    def test_all_four_are_built(self) -> None:
        names = [t.name for t in build_kb_tools(kb_config())]
        assert names == [
            "kb_vector_search",
            "kb_fts_search",
            "kb_doc_vector_search",
            "kb_doc_fts_search",
        ]

    def test_search_arguments(self) -> None:
        tool = build_kb_tools(kb_config())[0]
        assert set(tool.args) == {"query", "top_k", "snippet_chars"}


class TestConfluenceTools:
    def test_all_four_are_built(self) -> None:
        cfg = ConfluenceToolsConfig(
            confluence=HttpProfile(base_url="https://confluence.example")
        )
        names = [t.name for t in build_confluence_tools(cfg)]
        assert names == [
            "confluence_fetch",
            "confluence_grep",
            "confluence_search",
            "confluence_spaces",
        ]

    def test_network_error_becomes_error_result(self) -> None:
        cfg = ConfluenceToolsConfig(
            confluence=HttpProfile(base_url="http://127.0.0.1:1")
        )
        tool = next(
            t for t in build_confluence_tools(cfg) if t.name == "confluence_fetch"
        )
        result = invoke(tool, {"page_id": "1"})
        assert isinstance(result, ErrorResult)
        assert result.ok is False
