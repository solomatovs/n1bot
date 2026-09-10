"""Тесты перенесённых инструментов: pg и kb."""

from __future__ import annotations

import os
from typing import ClassVar

import pytest

from boba.sandbox import SandboxToolConfig
from boba.stand.sandbox import ROOTFS_IMAGE
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.pg.tools import PgToolConfig


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def pg_config() -> PgToolConfig:
    return PgToolConfig.model_validate({"max_rows": 10, "sandbox": _SANDBOX})


def kb_config() -> PostgresKnowledgeBaseConfig:
    return PostgresKnowledgeBaseConfig.model_validate(
        {
            "connection": {
                "host": "h",
                "dbname": "d",
                "auth": {"method": "trust", "user": "u"},
            },
            "tables": {"pg_schema": "kb"},
            "embedding": {
                "kind": "local",
                "model": "intfloat/multilingual-e5-small",
                "dim": 384,
                "batch_size": 8,
                "progress_every": 1,
            },
            "sandbox": _SANDBOX,
        }
    )


class TestPgTools:
    pytestmark = pytest.mark.anyio

    _NAMES: ClassVar[list[str]] = [
        "pg_list_tables",
        "pg_describe_table",
        "pg_query",
        "pg_copy",
        "pg_copy_out",
        "pg_copy_in",
    ]

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in PG_TOOLS]
        if names != self._NAMES:
            raise AssertionError(f"names == self._NAMES, got {names}")

    def test_every_tool_takes_a_connection_parameter(self) -> None:
        """Профиль подаёт хост: у каждого инструмента параметр с маркером."""
        from boba.db.postgres.profile import PostgresConfig
        from boba.toolkit.entry import ToolArgv

        for payload in PG_TOOLS:
            fields = ToolArgv.connection_fields(payload.args_schema)
            if list(fields) != ["connection"]:
                raise AssertionError(f"{payload.name}: connection parameter is missing")

            if fields["connection"] is not PostgresConfig:
                raise AssertionError(f"{payload.name}: profile type must be declared")

    def test_section_config_holds_limits_only(self) -> None:
        """Whitelist ушёл на хост: в секции остались только границы выдачи."""
        cfg = pg_config()
        if cfg.max_rows != 10:
            raise AssertionError("section keys must reach the model")

        if hasattr(cfg, "profiles"):
            raise AssertionError("profiles must not live in the section any more")


class TestKbTools:
    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in KB_TOOLS]
        if not (
            names
            == [
                "kb_vector_search",
                "kb_fts_search",
            ]
        ):
            raise AssertionError('names == [ "kb_vector_search", "kb_fts_search", ]')

    def test_search_arguments_hide_injected(self) -> None:
        tool = KB_TOOLS[0]
        llm_fields = set(tool.args_schema.model_fields) - {"cfg"}
        if llm_fields != {"query", "top_k"}:
            raise AssertionError(
                f'llm_fields == {{"query", "top_k"}}, got {llm_fields}'
            )


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


_PROFILE_RAW: dict[str, object] = {
    "host": {
        "mounting": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
        "stderr_tail_bytes": 4096,
        "channel_limit_bytes": 67108864,
        "fail_tail_chars": 2000,
        "kill_grace_sec": 5,
        "cgroup_base": "",
    },
    "rootfs": str(ROOTFS_IMAGE),
    "mounts": {
        "ro": (),
        "rw": (),
        "tmp": "64M",
    },
    "isolation": {
        "network": False,
        "env": {"PATH": "/usr/bin:/bin"},
        "reap_poll_sec": 0.05,
    },
    "limits": {
        "timeout_sec": 30,
        "process_memory_bytes": 512 * 1024 * 1024,
        "process_cpu_sec": 30,
        "process_file_bytes": 64 * 1024 * 1024,
        "process_open_files": 1024,
        "process_oom_score_adj": 0,
    },
    "run": {
        "cwd": "/tmp",  # noqa: S108
    },
}

_SANDBOX = SandboxToolConfig.model_validate({"profile": _PROFILE_RAW})
