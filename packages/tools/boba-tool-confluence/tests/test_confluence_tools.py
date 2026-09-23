"""Инструменты чтения Confluence: состав набора, ошибка слоя и строка спейса."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from boba.confluence.models import ConfluencePayloadError
from boba.confluence.rest import CflRestBuilder, SpaceType
from boba.indexing import TransportError
from boba.tool.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.tool.confluence.tools import (
    ConfluenceToolsConfig,
    SpaceList,
)
from boba.toolkit.entry import ToolMain
from boba.transport.http.connection import HttpConnection, UrlScheme

# порт 1 закрыт всегда: тест проверяет ошибку соединения, а не адрес


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


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
                "confluence_address",
            ]
        ):
            raise AssertionError('names == [ "confluence_fetch", "confluence_grep", "…')

    async def test_network_error_raises_domain_error(self) -> None:
        # класс ошибки берётся из того же модуля, что и тело: соседние тесты
        # перезагружают модуль инструментов, и класс с import'а модуля устаревает
        import boba.tool.confluence.tools as confluence_tools

        cfg = ConfluenceToolsConfig(
            confluence=HttpConnection(scheme=UrlScheme.HTTP, host="127.0.0.1", port=1),
        )

        body = ToolMain.toolset(confluence_tools.confluence_fetch)[0].coroutine
        if body is None:
            raise AssertionError("body is not None")
        with pytest.raises(TransportError):
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
        [space, _] = SpaceList(None, self.PROFILE).items(
            self.ANSWER, CflRestBuilder().space_list_path(SpaceType.ANY)
        )

        row = SpaceList(None, self.PROFILE).row(space)

        if row["url"] != "https://confluence.example.local/display/DQ":
            raise AssertionError(f"unexpected url: {row}")
        if row["key"] != "DQ" or row["name"] != "Качество данных":
            raise AssertionError(f"unexpected row: {row}")

    def test_space_without_webui_falls_back_to_the_service_root(self) -> None:
        [_, bare] = SpaceList(None, self.PROFILE).items(
            self.ANSWER, CflRestBuilder().space_list_path(SpaceType.ANY)
        )

        row = SpaceList(None, self.PROFILE).row(bare)

        if row["url"] != "https://confluence.example.local":
            raise AssertionError(f"unexpected url: {row}")

    def test_pattern_matches_key_or_name(self) -> None:
        [space, _] = SpaceList(None, self.PROFILE).items(
            self.ANSWER, CflRestBuilder().space_list_path(SpaceType.ANY)
        )

        if not SpaceList(None, self.PROFILE).matches(space):
            raise AssertionError("no pattern takes every space")
        if not SpaceList("d*", self.PROFILE).matches(space):
            raise AssertionError("the key matches the glob")
        if not SpaceList("*данных*", self.PROFILE).matches(space):
            raise AssertionError("the name matches the glob")
        if SpaceList("PHDD*", self.PROFILE).matches(space):
            raise AssertionError("a foreign glob must not match")

    def test_broken_results_raise_the_layer_error(self) -> None:
        import boba.tool.confluence.tools as confluence_tools

        with pytest.raises(ConfluencePayloadError, match="space"):
            confluence_tools.SpaceList(None, self.PROFILE).items(
                {"results": [{"name": "no key here"}]},
                CflRestBuilder().space_list_path(SpaceType.ANY),
            )
