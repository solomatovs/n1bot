"""Tool web_fetch: окно строк веб-страницы.

Скачивает и режет окно payload внутри песочницы; здесь остаётся проверка
хоста по whitelist'у и упаковка ответа для LLM.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.chainlit2.agent.tools.web.caller import WebCaller
from boba.chainlit2.agent.tools.web.connection import WebConnection

__all__ = ["web_fetch"]


def web_fetch(  # noqa: PLR0913 — окно строк задаётся явными параметрами
    cfg: WebConnection,
    caller: WebCaller,
    url: Annotated[
        str,
        Field(min_length=1, description="URL для скачивания"),
    ],
    as_markdown: Annotated[
        bool,
        Field(description="true — конвертирует HTML->Markdown"),
    ],
    line_offset: Annotated[
        int,
        Field(ge=0, description="Вернуть контент начиная со строки `line_offset`"),
    ],
    line_count: Annotated[
        int,
        Field(ge=1, description="Сколько строк вернуть начиная с `line_offset`"),
    ],
) -> dict[str, Any]:
    """Скачать URL и вернуть окно строк его содержимого."""
    profile = cfg.resolve_profile(url)
    answer = caller.fetch(
        url=url,
        profile=profile,
        as_markdown=as_markdown,
        line_offset=line_offset,
        line_count=line_count,
    )
    return answer.as_payload()
