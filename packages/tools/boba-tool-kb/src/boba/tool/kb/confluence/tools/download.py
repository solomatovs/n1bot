"""Tool `confluence_download` + `ConfluenceDownloadConfig` (unified HTTP-download).

Один tool, два режима — LLM заполняет РОВНО ОДИН из дискриминирующих
параметров (space_keys / page_ids):

- `space_keys=[A, B]`   → все страницы перечисленных Confluence-spaces
                          (discovery `/rest/api/space/{key}/content`).
- `page_ids=[123, 456]` → явный список страниц по ID.

`as_markdown=True` конвертирует HTML в Markdown (`markdownify`, ATX-заголовки)
и пишет `.md` с YAML-frontmatter; иначе — `.html` с HTML-комментарием-header'ом.

Конфигурация (confluence/dest_dir) — из TOML-секции
`[tool.kb.confluence.download]`.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated, Any

from pydantic import Field

from boba.indexing import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.attachments import AttachmentFilter
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources import (
    ConfluenceMultiSpaceRequestSource,
    ConfluencePagesRequestSource,
)
from boba.tools import FromConfig, tool
from boba.tools.domain import ToolProgressReported

__all__ = ["ConfluenceDownloadConfig", "confluence_download"]

_PIPELINE_ID: PipelineId = PipelineId("confluence.download")


class ConfluenceDownloadConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_download`.

    Config-секция: `[tool.kb.confluence.download]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.download",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection
    dest_dir: str = Field(
        min_length=1,
        description=(
            "Директория внутри workspace для сохранения "
            "(создаётся, если не существует)."
        ),
    )
    attachment_media_types: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.media_type` "
                "(напр. `application/pdf`, `image/*`). Если пусто И "
                "`attachment_titles` пуст — скачиваются ВСЕ вложения "
                "(старое поведение). Если задано — attachment проходит, "
                "если матчит хоть один паттерн в любом из двух списков (OR). "
                "Отсеянные вложения не запрашиваются по HTTP и не пишутся на диск."
            ),
        ),
    ] = []  # noqa: RUF012
    attachment_titles: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.title` "
                "(напр. `*.pdf`, `report-*.docx`). См. `attachment_media_types`."
            ),
        ),
    ] = []  # noqa: RUF012


@tool
def confluence_download(
    cfg: Annotated[ConfluenceDownloadConfig, FromConfig()],
    space_keys: Annotated[
        list[str] | None,
        Field(
            description=(
                "Mode A: скачать все страницы перечисленных Confluence-spaces."
            ),
        ),
    ] = None,
    page_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "Mode B: скачать список page_id. Каждый id — строка из "
                "URL `viewpage.action?pageId=<id>`. Используй, если уже знаешь "
                "конкретные страницы. Взаимоисключающий с `space_keys`."
            ),
        ),
    ] = None,
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown (`markdownify`, "
                "ATX-заголовки) и пишет `.md` с YAML-frontmatter. По умолчанию "
                "сохраняется HTML с `<!--`-frontmatter."
            ),
        ),
    ] = False,
) -> Generator[ToolProgressReported, None, dict[str, Any]]:
    """Confluence-download: spaces / page_ids по выбору LLM.

    LLM заполняет ровно один из (`space_keys`, `page_ids`).

    По ходу выполнения yield-ит `ToolProgressReported` per saved-record
    (page / attachment). Через `return` отдаёт финальный JSON
    `{mode, <discriminator>, dest_dir, saved, total}` —
    `DishkaTool` оборачивает в `ToolStreamCompleted` автоматически.
    """
    modes = [
        ("space_keys", space_keys),
        ("page_ids", page_ids),
    ]
    chosen = [(name, val) for name, val in modes if val]
    if len(chosen) != 1:
        provided = [name for name, _ in chosen] or ["<none>"]
        msg = (
            "confluence_download: задай РОВНО ОДИН из (space_keys, page_ids); "
            f"получено {len(chosen)}: {provided}"
        )
        raise ValueError(msg)

    mode_name, mode_value = chosen[0]
    if mode_name == "space_keys":
        request_source = ConfluenceMultiSpaceRequestSource(
            conn=cfg.confluence,
            space_keys=mode_value,
            body_format=cfg.confluence.body_format,
        )
    else:  # page_ids
        request_source = ConfluencePagesRequestSource(
            base_url=cfg.confluence.base_url,
            auth=cfg.confluence.make_auth(),
            page_ids=mode_value,
            body_format=cfg.confluence.body_format,
        )

    att_filter = AttachmentFilter.from_lists(
        media_types=cfg.attachment_media_types,
        titles=cfg.attachment_titles,
    )
    stats = yield from download_pages(
        request_source=request_source,
        conn=cfg.confluence,
        dest_dir=cfg.dest_dir,
        as_markdown=as_markdown,
        pipeline_id=_PIPELINE_ID,
        attachment_filter=att_filter,
    )
    return {"mode": mode_name, mode_name: mode_value, **stats}
