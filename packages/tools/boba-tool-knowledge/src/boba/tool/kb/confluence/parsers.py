"""Разборщики Confluence, исполняемые в песочнице: документы и страницы."""

from __future__ import annotations

from dataclasses import dataclass

from boba.tool.doc.liteparse import LiteParseCaller, SandboxParserConfig
from boba.tool.kb.html import HtmlCaller
from boba.toolkit.launcher import LauncherFactory

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
        launchers: LauncherFactory,
    ) -> ConfluenceParsers:
        """Оба разборщика идут в одну песочницу — точка входа у них общая."""
        return cls(
            documents=LiteParseCaller(tool, cfg, launchers),
            pages=HtmlCaller(tool, launchers),
        )
