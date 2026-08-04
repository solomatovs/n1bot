"""Confluence-специфичные CleanupStrategy для force-reindex отдельных страниц."""

from __future__ import annotations

import re
from typing import ClassVar

from boba.indexing import (
    CleanupContext,
    CleanupStrategy,
    TrackingKeys,
)
from boba.indexing.filter import And, Filter, In, Lt
from boba.tool.kb.confluence.models import ConfluenceKeys

__all__ = ["ConfluencePageScopeCleanup"]


class ConfluencePageScopeCleanup(CleanupStrategy):
    """Удаляет stale-чанки в scope «страница + вложения» через In(page_id): снятые
    вложения source_id-scoped IncrementalCleanup не достаёт, page_id — достаёт."""

    _PAGE_ID_RE: ClassVar[re.Pattern[str]] = re.compile(r"/content/([^/?#]+)$")

    async def execute(self, ctx: CleanupContext) -> int:
        page_ids = sorted({
            match.group(1)
            for source in ctx.touched_sources
            if (match := self._PAGE_ID_RE.search(str(source)))
        })
        if not page_ids:
            return 0
        where: Filter = And([
            In(ConfluenceKeys.PAGE_ID.name, page_ids),
            Lt(TrackingKeys.UPDATED_AT, ctx.run_start),
        ])
        return await ctx.query.clean(where=where)
