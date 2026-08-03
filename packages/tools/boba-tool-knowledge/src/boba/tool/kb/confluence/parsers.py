"""Разборщики Confluence, исполняемые в песочнице: документы и страницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from boba.tool.doc.liteparse import LiteParseCaller, SandboxParserConfig
from boba.tool.kb.html import HtmlCaller

__all__ = ["ConfluenceParsers"]


@dataclass(frozen=True)
class ConfluenceParsers:
    """Пара payload-вызовов: вложения (liteparse) и HTML-страницы."""

    documents: LiteParseCaller
    pages: HtmlCaller

    @classmethod
    def of(
        cls,
        tool: str,
        cfg: SandboxParserConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> ConfluenceParsers:
        """Оба разборщика идут в одну песочницу — точка входа у них общая."""
        return cls(
            documents=LiteParseCaller(tool, cfg, path_vars),
            pages=HtmlCaller(tool, cfg.sandbox, path_vars),
        )
