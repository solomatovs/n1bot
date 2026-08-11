"""Тесты перенесённых инструментов: pg, kb, confluence."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from conftest import RecordingLauncher
from pydantic import SecretStr

from boba.sandbox import SandboxToolConfig
from boba.tool.kb import (
    PostgresKnowledgeBaseConfig,
    build_kb_tools,
)
from boba.tool.kb.confluence import (
    ConfluenceToolsConfig,
    build_confluence_tools,
)
from boba.tool.kb.confluence.protocol import (
    ConfluenceGrepRequest,
    ConfluenceNode,
    ConfluencePageRequest,
    ConfluencePageTrailer,
    ConfluenceSpacesRequest,
)
from boba.tool.kb.confluence.stages import ConfluenceStages
from boba.tool.kb.protocol import KbNode, KbSearchRequest
from boba.tool.kb.stages import KbStages
from boba.tool.pg import (
    PgExecutorConfig,
    build_pg_tools,
)
from boba.tool.pg import executor as pg_executor
from boba.toolkit.launcher import LauncherError
from boba.toolkit.result import (
    ErrorResult,
    TableResult,
    ToolArtifact,
)
from boba.toolkit.sql import SqlQueryError
from boba.toolkit.workflow import EmptyTrailer
from boba.transport.http import BearerAuth, HttpProfile


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class _DeadLauncher:
    """Исполнитель, у которого запуск всегда срывается: проверяется обвязка."""

    def call(self, spec: Any, sinks: Any = None) -> Any:
        raise LauncherError("песочница недоступна")


def _no_launcher(tool: str) -> Any:
    return _DeadLauncher()


def pg_config() -> PgExecutorConfig:
    return PgExecutorConfig.model_validate(
        {
            "profiles": {"main": {"host": "h", "dbname": "d", "user": "u"}},
            "sandbox": _SANDBOX,
        }
    )


def kb_secret_config() -> PostgresKnowledgeBaseConfig:
    """Тот же конфиг, но с паролем: проверка раскрытия секретов."""
    return PostgresKnowledgeBaseConfig.model_validate(
        {
            "connection": {
                "host": "h",
                "dbname": "d",
                "user": "u",
                "password": "s3cret",
            },
            "tables": {"pg_schema": "kb"},
            "embedding": {
                "model": "intfloat/multilingual-e5-small",
                "dim": 384,
                "batch_size": 8,
            },
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


async def ainvoke(tool: Any, args: dict[str, Any]) -> Any:
    """pg-инструменты асинхронные: sync-вызова у них нет по построению."""
    message = await tool.ainvoke({"name": tool.name, "args": args, "id": "c1",
                                  "type": "tool_call"})
    return ToolArtifact.revive(message.artifact)


class TestPgTools:
    pytestmark = pytest.mark.anyio

    _NAMES: ClassVar[list[str]] = [
        "pg_list_targets",
        "pg_list_tables",
        "pg_describe_table",
        "pg_query",
        "pg_export",
    ]

    def test_all_five_are_built(self) -> None:
        names = [t.name for t in build_pg_tools(pg_config(), _no_launcher)]
        assert names == self._NAMES

    async def test_list_targets_returns_whitelist(self) -> None:
        tool = build_pg_tools(pg_config(), _no_launcher)[0]
        result = await ainvoke(tool, {})
        assert isinstance(result, TableResult)
        assert list(result.rows) == [{"connection_name": "main"}]
        assert result.ok is True

    async def test_unknown_target_becomes_error_result(self) -> None:
        """Профиль не в whitelist — ошибка инструмента, а не падение хода."""
        for name in ("pg_list_tables", "pg_describe_table", "pg_query", "pg_export"):
            built = build_pg_tools(pg_config(), _no_launcher)
            tool = next(t for t in built if t.name == name)
            args: dict[str, Any] = {"connection_name": "нет-такого"}
            if name == "pg_describe_table":
                args["table"] = "t"
            if name in ("pg_query", "pg_export"):
                args["sql"] = "select 1"
            result = await ainvoke(tool, args)
            assert isinstance(result, ErrorResult), name
            assert result.error_kind == "unknown_target", name
            assert result.ok is False, name

    async def test_sql_error_becomes_error_result(self, monkeypatch) -> None:
        async def boom(*_args: Any, **_kwargs: Any):
            raise SqlQueryError("relation does not exist")

        monkeypatch.setattr(pg_executor.PgExecutor, "execute", boom)
        built = build_pg_tools(pg_config(), _no_launcher)
        tool = next(t for t in built if t.name == "pg_list_tables")
        result = await ainvoke(tool, {"connection_name": "main"})
        assert isinstance(result, ErrorResult)
        assert result.ok is False
        assert "relation does not exist" in result.message


class TestKbTools:
    @staticmethod
    def _launcher(cfg: PostgresKnowledgeBaseConfig) -> RecordingLauncher:
        nodes = KbStages.of(cfg)
        trailers = {
            KbNode.VECTOR.value: EmptyTrailer(),
            KbNode.FTS.value: EmptyTrailer(),
        }
        return RecordingLauncher(nodes, trailers)

    def test_search_args_become_a_full_request(self) -> None:
        """LLM даёт запрос и лимиты, обогатитель — соединение, таблицы и модель."""
        cfg = kb_config()
        launcher = self._launcher(cfg)
        tool = build_kb_tools(cfg, lambda _tool: launcher)[0]

        invoke(tool, {"query": "как считается витрина", "top_k": 3})

        request = launcher.requests[0]
        assert isinstance(request, KbSearchRequest)
        assert request.op is KbNode.VECTOR
        assert request.query == "как считается витрина"
        assert request.top_k == 3
        assert list(request.collections) == ["kb_confluence"]
        assert request.chunks_table == "kb_chunks"
        assert request.connection.dbname == "d"
        assert request.embedding.model == "intfloat/multilingual-e5-small"

    def test_secrets_are_revealed_only_in_the_request_json(self) -> None:
        """Пароль соединения виден только в JSON запроса, не в дампе модели."""
        cfg = kb_secret_config()
        launcher = self._launcher(cfg)
        tool = build_kb_tools(cfg, lambda _tool: launcher)[0]

        invoke(tool, {"query": "тест"})

        request = launcher.requests[0]
        assert isinstance(request, KbSearchRequest)
        assert "s3cret" not in str(request.model_dump())
        assert "s3cret" in request.model_dump_json()

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
            "lock_wait_sec": 10.0,
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


class TestConfluenceRequests:
    """args фасада плюс конфиг секции складываются в запрос узла."""

    @staticmethod
    def _cfg() -> ConfluenceToolsConfig:
        return ConfluenceToolsConfig(
            confluence=HttpProfile(
                base_url="https://confluence.example",
                auth=BearerAuth(method="bearer", token=SecretStr("t0ken")),
            ),
            max_text_chars=1500,
            page_size=400,
        )

    @classmethod
    def _launcher(cls) -> RecordingLauncher:
        nodes = ConfluenceStages.of(cls._cfg())
        trailers: dict[str, Any] = {
            ConfluenceNode.PAGE.value: ConfluencePageTrailer(title=""),
            ConfluenceNode.GREP.value: EmptyTrailer(),
            ConfluenceNode.SEARCH.value: EmptyTrailer(),
            ConfluenceNode.SPACES.value: EmptyTrailer(),
        }
        return RecordingLauncher(nodes, trailers)

    def _invoke(self, name: str, args: dict[str, Any]) -> RecordingLauncher:
        launcher = self._launcher()
        built = build_confluence_tools(self._cfg(), lambda _tool: launcher)
        tool = next(t for t in built if t.name == name)
        invoke(tool, args)
        return launcher

    def test_page_request_carries_connection(self) -> None:
        launcher = self._invoke("confluence_fetch", {"page_id": "42"})

        request = launcher.requests[0]
        assert isinstance(request, ConfluencePageRequest)
        assert request.page_id == "42"
        assert request.base_url == "https://confluence.example"
        assert request.body_format == "view"
        assert request.as_markdown is True

    def test_page_token_is_revealed_only_in_json(self) -> None:
        launcher = self._invoke("confluence_fetch", {"page_id": "42"})

        request = launcher.requests[0]
        assert "t0ken" not in str(request.model_dump())
        assert "t0ken" in request.model_dump_json()

    def test_grep_limits_come_from_the_section(self) -> None:
        launcher = self._invoke(
            "confluence_grep", {"page_id": "42", "pattern": "alpha"}
        )

        request = launcher.requests[0]
        assert isinstance(request, ConfluenceGrepRequest)
        assert request.max_text_chars == 1500
        assert request.pattern == "alpha"

    def test_spaces_page_size_comes_from_the_section(self) -> None:
        launcher = self._invoke("confluence_spaces", {"space_type": "global"})

        request = launcher.requests[0]
        assert isinstance(request, ConfluenceSpacesRequest)
        assert request.limit == 400
        assert request.space_type == "global"


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

    def test_launch_failure_becomes_error_result(self) -> None:
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
