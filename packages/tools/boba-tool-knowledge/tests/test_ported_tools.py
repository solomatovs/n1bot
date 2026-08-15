"""Тесты перенесённых инструментов: pg, kb, confluence."""

from __future__ import annotations

import os
from typing import Any, ClassVar

import pytest

from boba.sandbox import SandboxToolConfig
from boba.tool.kb.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.tool.kb.confluence.tools import (
    ConfluenceRequestError,
    ConfluenceToolsConfig,
)
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.pg.tools import PgToolConfig, pg_list_targets
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import (
    TableResult,
    ToolArtifact,
)
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


def pg_config() -> PgToolConfig:
    return PgToolConfig.model_validate(
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
    message = tool.invoke(
        {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
    )
    return ToolArtifact.revive(message.artifact)


async def ainvoke(tool: Any, args: dict[str, Any]) -> Any:
    """pg-инструменты асинхронные: sync-вызова у них нет по построению."""
    message = await tool.ainvoke(
        {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
    )
    return ToolArtifact.revive(message.artifact)


class TestPgTools:
    pytestmark = pytest.mark.anyio

    _NAMES: ClassVar[list[str]] = [
        "pg_list_targets",
        "pg_list_tables",
        "pg_describe_table",
        "pg_query",
        "pg_copy",
    ]

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in PG_TOOLS]
        assert names == self._NAMES

    async def test_list_targets_returns_whitelist(self) -> None:
        body = ToolMain.toolset(pg_list_targets)[0].coroutine
        assert body is not None
        _content, artifact = await body(cfg=pg_config())

        assert isinstance(artifact, TableResult)
        assert list(artifact.rows) == [{"connection_name": "main"}]
        assert artifact.ok is True

    async def test_unknown_target_raises_domain_error(self) -> None:
        """Профиль не в whitelist — доменное исключение с kind в EXPECTED."""
        from boba.tool.pg.tools import EXPECTED, pg_query
        from boba.toolkit.entry import ExpectedErrors
        from boba.toolkit.sql import UnknownConnectionError

        body = ToolMain.toolset(pg_query)[0].coroutine
        assert body is not None
        with pytest.raises(UnknownConnectionError) as caught:
            await body(connection_name="нет-такого", sql="select 1", cfg=pg_config())

        kind = ExpectedErrors.kind_of(caught.value, dict(EXPECTED))
        assert kind == "unknown_target"


class TestKbTools:
    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in KB_TOOLS]
        assert names == [
            "kb_vector_search",
            "kb_fts_search",
        ]

    def test_search_arguments_hide_injected(self) -> None:
        tool = KB_TOOLS[0]
        llm_fields = set(tool.args_schema.model_fields) - {"cfg"}
        assert llm_fields == {"query", "top_k", "snippet_chars"}


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


_SANDBOX = SandboxToolConfig.model_validate(
    {
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
            "binaries": {"dirs": _bin_dirs()},
            "tmpfs": ("/tmp:64M",),  # noqa: S108
            "network": False,
            "env_set": {"PATH": "/usr/bin:/bin"},
            "timeout_sec": 30,
            "max_memory_bytes": 512 * 1024 * 1024,
            "max_cpu_sec": 30,
            "max_file_size_bytes": 64 * 1024 * 1024,
            "max_open_files": 1024,
            "max_processes": 256,
            "cgroup_base": "",
            "oom_score_adj": 0,
            "cwd": "/tmp",  # noqa: S108
        },
        "override": {},
    }
)


class TestConfluenceTools:
    pytestmark = pytest.mark.anyio

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in CONFLUENCE_TOOLS]
        assert names == [
            "confluence_fetch",
            "confluence_grep",
            "confluence_search",
            "confluence_spaces",
        ]

    async def test_network_error_raises_domain_error(self) -> None:
        from boba.tool.kb.confluence.tools import confluence_fetch

        cfg = ConfluenceToolsConfig(
            confluence=HttpProfile(base_url="http://127.0.0.1:1"),
        )

        body = ToolMain.toolset(confluence_fetch)[0].coroutine
        assert body is not None
        with pytest.raises(ConfluenceRequestError):
            await body(page_id="1", cfg=cfg)
