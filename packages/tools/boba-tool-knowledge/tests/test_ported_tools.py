"""Тесты перенесённых инструментов: pg, kb, confluence."""

from __future__ import annotations

import os
from typing import Any, ClassVar

import pytest

from boba.sandbox import SandboxToolConfig
from boba.stand.sandbox import ROOTFS_IMAGE
from boba.tool.kb.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.tool.kb.confluence.tools import (
    ConfluenceToolsConfig,
    SpaceList,
)
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.pg.tools import PgToolConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import (
    ToolArtifact,
)
from boba.transport.http.profile import HttpConnection, UrlScheme

# порт 1 закрыт всегда: тест проверяет ошибку соединения, а не адрес


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


class TestConfluenceTools:
    pytestmark = pytest.mark.anyio

    def test_module_declares_the_toolset(self) -> None:
        names = [t.name for t in CONFLUENCE_TOOLS]
        if not (
            names
            == [
                "confluence_fetch",
                "confluence_grep",
                "confluence_search",
                "confluence_spaces",
            ]
        ):
            raise AssertionError('names == [ "confluence_fetch", "confluence_grep", "…')

    async def test_network_error_raises_domain_error(self) -> None:
        # класс ошибки берётся из того же модуля, что и тело: соседние тесты
        # перезагружают модуль инструментов, и класс с import'а модуля устаревает
        import boba.tool.kb.confluence.tools as confluence_tools

        cfg = ConfluenceToolsConfig(
            confluence=HttpConnection(scheme=UrlScheme.HTTP, host="127.0.0.1", port=1),
        )

        body = ToolMain.toolset(confluence_tools.confluence_fetch)[0].coroutine
        if body is None:
            raise AssertionError("body is not None")
        with pytest.raises(confluence_tools.ConfluenceRequestError):
            await body(page_id="1", cfg=cfg)


class TestSpaceList:
    """Строка спейса: ключ, название, тип и адрес для перехода."""

    PROFILE: ClassVar[HttpConnection] = HttpConnection(
        host="confluence.example.local", port=443
    )

    ANSWER: ClassVar[dict[str, Any]] = {
        "results": [
            {
                "key": "DQ",
                "name": "Качество данных",
                "type": "global",
                "_links": {"webui": "/display/DQ"},
            },
            {"key": "BARE", "name": "Без ссылки", "type": "personal"},
        ]
    }

    def test_row_carries_the_space_url(self) -> None:
        [space, _] = SpaceList.items(self.ANSWER, "/rest/api/space")

        row = SpaceList.row(space, self.PROFILE)

        if row["url"] != "https://confluence.example.local/display/DQ":
            raise AssertionError(f"unexpected url: {row}")
        if row["key"] != "DQ" or row["name"] != "Качество данных":
            raise AssertionError(f"unexpected row: {row}")

    def test_space_without_webui_falls_back_to_the_service_root(self) -> None:
        [_, bare] = SpaceList.items(self.ANSWER, "/rest/api/space")

        row = SpaceList.row(bare, self.PROFILE)

        if row["url"] != "https://confluence.example.local":
            raise AssertionError(f"unexpected url: {row}")

    def test_pattern_matches_key_or_name(self) -> None:
        [space, _] = SpaceList.items(self.ANSWER, "/rest/api/space")

        if not SpaceList.matches(space, None):
            raise AssertionError("no pattern takes every space")
        if not SpaceList.matches(space, "d*"):
            raise AssertionError("the key matches the glob")
        if not SpaceList.matches(space, "*данных*"):
            raise AssertionError("the name matches the glob")
        if SpaceList.matches(space, "PHDD*"):
            raise AssertionError("a foreign glob must not match")

    def test_broken_results_raise_the_layer_error(self) -> None:
        import boba.tool.kb.confluence.tools as confluence_tools

        with pytest.raises(confluence_tools.ConfluenceRequestError, match="space"):
            confluence_tools.SpaceList.items(
                {"results": [{"name": "no key here"}]}, "/rest/api/space"
            )
