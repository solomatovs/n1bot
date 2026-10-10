"""Пометки инструментов плагинов: каждый инструмент каждого манифеста
`boba.tools` размечен сознательно — описание, поиск, чтение и адрес читают,
произвольный SQL, оболочка, почта и удаление разрушают, остальные меняют без
разрушения. Без пометки клиент спрашивал бы разрешение на каждое чтение."""

from __future__ import annotations

import re
from importlib.metadata import entry_points

from boba.toolkit.facade import PayloadTool
from boba.toolkit.manifest import ToolPluginManifest

READ_ONLY = re.compile(
    r"(_describe|_describe_|_list_|_list$|_search|_address$|_fetch|_grep|_spaces$"
    r"|_catalog|_node2$|_structure$|_descriptions$|_stream_out$"
    r"|^read_document$|^document_outline$|^search_)"
)
DESTRUCTIVE = {"pg_query", "ch_query", "ora_query", "bash", "mail"}
DELETES = re.compile(r"_delete_")


def plugin_tools() -> list[PayloadTool]:
    tools: list[PayloadTool] = []
    for entry in entry_points(group=ToolPluginManifest.GROUP):
        manifest = entry.load()
        assert isinstance(manifest, ToolPluginManifest), entry.value
        tools.extend(manifest.tools)

    return tools


class TestToolHints:
    def test_every_plugin_tool_is_marked_by_its_kind(self) -> None:
        tools = plugin_tools()
        assert tools

        for payload in tools:
            name = payload.name
            hints = payload.hints
            if name in DESTRUCTIVE or DELETES.search(name):
                assert not hints.read_only, name
                assert hints.destructive, name
                continue

            if READ_ONLY.search(name):
                assert hints.read_only, name
                assert not hints.destructive, name
                continue

            assert not hints.read_only, name
            assert not hints.destructive, name
