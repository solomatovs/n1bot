"""Тесты перенесённых инструментов: pg и kb."""

from __future__ import annotations

import os
from typing import ClassVar

import pytest

from boba.sandbox import SandboxToolConfig
from boba.stand.sandbox import ROOTFS_IMAGE
from boba.tool.kb.kb import KbToolConfig
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.pg.tools import PgToolConfig


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def pg_config() -> PgToolConfig:
    return PgToolConfig.model_validate({"limit": 10, "sandbox": _SANDBOX})


def kb_config() -> KbToolConfig:
    return KbToolConfig.model_validate(
        {
            "connection": {
                "host": "h",
                "dbname": "d",
                "auth": {"method": "trust", "user": "u"},
            },
            "db_schema": "ix",
            "max_result_chars": 1000,
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
        "pg_address",
        "pg_database_describe",
        "pg_schema_describe",
        "pg_table_describe",
        "pg_column_describe",
        "pg_constraints_describe",
        "pg_indexes_describe",
        "pg_routines_describe",
        "pg_routine_arg_describe",
        "pg_sequences_describe",
        "pg_types_describe",
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
        if cfg.limit != 10:
            raise AssertionError("section keys must reach the model")

        if hasattr(cfg, "profiles"):
            raise AssertionError("profiles must not live in the section any more")


class TestKbTools:
    _NAMES: ClassVar[list[str]] = [
        "kb_vector_search",
        "kb_fts_search",
        "kb_catalog2",
        "kb_fts_search2",
        "kb_trgm_search2",
        "kb_vector_search2",
        "kb_node2",
    ]

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in KB_TOOLS]
        if names != self._NAMES:
            raise AssertionError(f"names == {self._NAMES}, got {names}")

    def test_search_arguments_hide_injected(self) -> None:
        expected = {"query", "surfaces", "aspects", "top_k"}
        for tool in KB_TOOLS:
            if not tool.name.endswith("_search2"):
                continue

            llm_fields = set(tool.args_schema.model_fields) - {"cfg"}
            if llm_fields != expected:
                raise AssertionError(f"{tool.name}: {expected}, got {llm_fields}")

    def test_chunks_search_arguments_hide_injected(self) -> None:
        by_name = {t.name: t for t in KB_TOOLS}
        for name in ("kb_vector_search", "kb_fts_search"):
            llm_fields = set(by_name[name].args_schema.model_fields) - {"cfg"}
            if llm_fields != {"query", "top_k"}:
                raise AssertionError(f'{name}: {{"query", "top_k"}}, got {llm_fields}')

    def test_node_arguments_hide_injected(self) -> None:
        by_name = {t.name: t for t in KB_TOOLS}
        llm_fields = set(by_name["kb_node2"].args_schema.model_fields) - {"cfg"}
        if llm_fields != {"node_id", "aspects"}:
            raise AssertionError(f'{{"node_id", "aspects"}}, got {llm_fields}')

    def test_config_binds_the_section(self) -> None:
        cfg = kb_config()
        if cfg.db_schema != "ix":
            raise AssertionError(f"db_schema == ix, got {cfg.db_schema}")


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
