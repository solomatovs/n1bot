"""Confluence-специфичные CleanupStrategy.

ConfluencePageScopeCleanup — удаление stale-чанков в пределах аггрегата
«страница + её вложения». Нужно для force-reindex отдельных страниц, где
source_id-scoped IncrementalCleanup не достаёт удалённые вложения.
"""

from __future__ import annotations

import re
from typing import ClassVar

from boba.chainlit2.agent.tools.confluence.models import ConfluenceKeys
from boba.indexing import (
    CleanupContext,
    CleanupStrategy,
    TrackingKeys,
)
from boba.indexing.filter import And, Filter, In, Lt

__all__ = ["ConfluencePageScopeCleanup"]


class ConfluencePageScopeCleanup(CleanupStrategy):
    """Удалить stale-чанки в пределах страниц, затронутых этим прогоном.

    Scope — аггрегат «страница + её вложения»: и текст страницы, и attachment-
    чанки несут confluence.page_id (вложения наследуют его от родителя при
    fan-out'е), поэтому In(page_id, touched) снимает и осиротевшие чанки самой
    страницы (ужалась), и удалённые с неё вложения — то, что source_id-scoped
    IncrementalCleanup не достаёт (source_id вложения не попадает в touched).
    Удаляется только не обновлённое в этом прогоне (updated_at < run_start);
    актуальное (свежий heartbeat от reconcile) переживает.

    touched page_id извлекаются из touched_sources: page-source_id — это
    `{base}/rest/api/content/{page_id}` (без query). source_id вложений в
    touched не попадают, поэтому ложных page_id здесь не будет.
    """

    _PAGE_ID_RE: ClassVar[re.Pattern[str]] = re.compile(r"/content/([^/?#]+)$")

    def execute(self, ctx: CleanupContext) -> int:
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
        return ctx.query.clean(where=where)
