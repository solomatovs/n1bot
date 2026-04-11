"""Адаптер импорта из Confluence — прямой REST API, запись на диск.

Скачивает HTML (export_view) страниц и пишет байты напрямую на диск
без промежуточного хранения в памяти.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List

import httpx

from domain.config import AppConfig
from domain.importing.confluence import (
    ConfluencePageQuery,
    ConfluenceSpaceQuery,
    ImportDone,
    ImportEvent,
    ImportPageFailed,
    ImportPageSaved,
    ImportSpaceEnumerated,
    extract_export_view_html,
    extract_page_ids,
    extract_page_title,
)
from domain.importing.loading import ConfluenceImportParams, SpaceLoadParams

log = logging.getLogger(__name__)


class ConfluenceImporter:
    """Скачивает страницы Confluence в export_view HTML и пишет на диск."""

    def __init__(self, cfg: AppConfig, params: ConfluenceImportParams) -> None:
        self._cfg = cfg
        self._params = params
        headers = self._resolve_auth_headers(cfg, params)
        self._client = httpx.Client(
            headers=headers,
            verify=params.ssl_verify,
            timeout=params.timeout,
        )

    @staticmethod
    def _resolve_auth_headers(cfg: AppConfig, params: ConfluenceImportParams) -> dict[str, str]:
        """Сформировать заголовки авторизации — пользовательский token или из конфига."""
        if params.token:
            return AppConfig.confluence_bearer_headers(params.token)
        return cfg.confluence_auth_headers

    def import_pages(
        self, page_ids: List[str], output_dir: Path,
    ) -> Iterator[ImportEvent]:
        """Скачать страницы по ID и записать на диск."""
        output_dir.mkdir(parents=True, exist_ok=True)
        total = len(page_ids)
        ok = 0
        failed = 0

        for idx, page_id in enumerate(page_ids, start=1):
            try:
                title = self._fetch_and_save(page_id, output_dir)
                ok += 1
                yield ImportPageSaved(
                    page_id=page_id, title=title,
                    file_path=str(_page_file_path(output_dir, page_id)),
                    index=idx, total=total,
                )
            except Exception as e:
                failed += 1
                log.warning("Failed to import page %s: %s", page_id, e)
                yield ImportPageFailed(
                    page_id=page_id, error=str(e), index=idx, total=total,
                )

        yield ImportDone(ok_count=ok, failed_count=failed, output_dir=str(output_dir))

    def import_space(
        self, space_key: str, space_params: SpaceLoadParams, output_dir: Path,
    ) -> Iterator[ImportEvent]:
        """Перечислить страницы пространства и скачать все."""
        page_ids = self._enumerate_space(space_key, space_params)

        if space_params.max_pages is not None and space_params.max_pages > 0:
            page_ids = page_ids[:space_params.max_pages]

        yield ImportSpaceEnumerated(space_key=space_key, total=len(page_ids))
        yield from self.import_pages(page_ids, output_dir)

    def close(self) -> None:
        self._client.close()

    # -- приватные методы ------------------------------------------------------

    def _fetch_and_save(self, page_id: str, output_dir: Path) -> str:
        """Скачать страницу и записать HTML на диск. Возвращает title.

        Confluence REST API возвращает JSON с HTML внутри —
        streaming невозможен, JSON парсится целиком.
        HTML записывается на диск сразу после извлечения.
        """
        url = self._cfg.confluence_page_url(page_id)
        query = ConfluencePageQuery()

        resp = self._client.get(url, params=query.to_params())
        resp.raise_for_status()
        data = resp.json()

        title = extract_page_title(data, fallback=page_id)
        html_value = extract_export_view_html(data)
        file_path = _page_file_path(output_dir, page_id)

        with open(file_path, "wb") as f:
            f.write(html_value.encode("utf-8"))

        return title

    def _enumerate_space(self, space_key: str, space_params: SpaceLoadParams) -> List[str]:
        """Получить список page_id из пространства через пагинацию."""
        limit = space_params.api_page_limit
        start = 0
        ids: List[str] = []

        while True:
            query = ConfluenceSpaceQuery(space_key=space_key, limit=limit, start=start)
            resp = self._client.get(self._cfg.confluence_content_url, params=query.to_params())
            resp.raise_for_status()
            batch = extract_page_ids(resp.json())
            if not batch:
                break
            ids.extend(batch)
            if len(batch) < limit:
                break
            start += len(batch)

        return ids


def _page_file_path(output_dir: Path, page_id: str) -> Path:
    """Путь к файлу импортированной страницы."""
    return output_dir / f"{page_id}.html"
