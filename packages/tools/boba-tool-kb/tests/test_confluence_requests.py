"""ConfluenceSpace/Pages/Cql RequestSources: REST URL'ы + viewpage source_id.

Integration: все тесты используют реальный `confluence_connection.base_url` из
`[tool.kb.confluence]` (по умолчанию `https://cwiki.apache.org/confluence`).
Часть тестов — URL-shape (только конструируют `RequestSource` без HTTP),
часть — реальная discovery через `[test.kb].confluence_space_key`/`_cql`.

`extract_host`-кейсы для разных base_url-форм (`/wiki/`, port) проверены
через `urlparse` напрямую — это pure-function из `_common.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from boba.indexing import PipelineContext
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluenceMultiSpaceRequestSource,
    ConfluencePagesRequestSource,
    ConfluenceSpaceRequestSource,
)
from boba.tool.kb.confluence.request_sources._common import (
    extract_host,
    viewpage_url,
)

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# URL-shape тесты (без HTTP-походов; используют реальный base_url + auth).
# --------------------------------------------------------------------------- #


def test_pages_source_sets_viewpage_source_id_and_rest_url(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    confluence_auth: httpx.Auth | None,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """RequestSource заполняет:
    - `url` = REST endpoint (для Transport),
    - `source_id` = stable viewpage URL (для identity).
    """
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids
    base = confluence_connection.base_url
    host = extract_host(base)

    src = ConfluencePagesRequestSource(
        base_url=base,
        auth=confluence_auth,
        page_ids=page_ids,
        body_format=confluence_connection.body_format,
    )
    requests = list(src.stream(pipeline_ctx))
    assert len(requests) == len(page_ids)
    assert all("rest/api/content/" in r.url for r in requests)
    assert all(f"expand=body.{confluence_connection.body_format}" in r.url for r in requests)
    # source_id — viewpage URL, отличается от REST url.
    for r, pid in zip(requests, page_ids, strict=True):
        assert r.source_id == viewpage_url(base, pid)
        assert r.source_id != r.url
        assert r.metadata.get(ConfluenceKeys.PAGE_ID) == pid
        assert r.metadata.get(ConfluenceKeys.HOST) == host
    if confluence_auth is None:
        assert all(r.auth is None for r in requests)
    else:
        assert all(r.auth is not None for r in requests)


def test_pages_request_url_includes_expand_for_body_format(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`?expand=body.<body_format>,...` шьётся в REST-URL по body_format-параметру."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_id = test_cfg.confluence_page_ids[0]
    # Берём storage-формат (отличается от operator-defаulta view) — чтобы
    # явно увидеть, что body_format действительно шьётся в URL.
    src = ConfluencePagesRequestSource(
        base_url=confluence_connection.base_url,
        auth=None,
        page_ids=[page_id],
        body_format="storage",
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert "expand=body.storage" in req.url
    # source_id не зависит от body_format — стабилен.
    assert req.source_id == viewpage_url(confluence_connection.base_url, page_id)


def test_request_url_strips_trailing_slash_in_base_url(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`base_url` с trailing `/` — корректно нормализуется (нет двойных `//`)."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_id = test_cfg.confluence_page_ids[0]
    src = ConfluencePagesRequestSource(
        base_url=confluence_connection.base_url + "/",  # явный trailing slash
        auth=None,
        page_ids=[page_id],
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert "//rest/api" not in req.url
    assert "//pages/viewpage" not in req.source_id


def test_metadata_carries_page_id_and_host(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """structured-данные (`PAGE_ID`, `HOST`) проставляются для downstream'а."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_id = test_cfg.confluence_page_ids[0]
    expected_host = extract_host(confluence_connection.base_url)

    src = ConfluencePagesRequestSource(
        base_url=confluence_connection.base_url,
        auth=None,
        page_ids=[page_id],
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert req.metadata.get(ConfluenceKeys.PAGE_ID) == page_id
    assert req.metadata.get(ConfluenceKeys.HOST) == expected_host


def test_real_base_url_host_extraction(
    confluence_connection: ConfluenceConnection,
) -> None:
    """`extract_host` на реальном `[tool.kb.confluence].base_url` — netloc only.

    Для Apache cwiki это `cwiki.apache.org` (без `/confluence` пути).
    """
    host = extract_host(confluence_connection.base_url)
    assert "://" not in host
    assert "/" not in host
    # Базовая sanity: hostname в base_url содержит то же.
    assert host in confluence_connection.base_url


# --------------------------------------------------------------------------- #
# Integration: реальная Confluence discovery (space-pagination + CQL-search).
# --------------------------------------------------------------------------- #


def test_space_source_returns_pages_from_real_space(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальная discovery: SpaceRequestSource эмитит HttpRequest'ы со
    стабильным viewpage source_id и REST URL для каждой страницы space-а.
    """
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    src = ConfluenceSpaceRequestSource(
        conn=confluence_connection,
        space_key=test_cfg.confluence_space_key,
        body_format=confluence_connection.body_format,
    )
    requests = list(src.stream(pipeline_ctx))
    assert requests, f"space {test_cfg.confluence_space_key!r} вернул 0 страниц"
    base = confluence_connection.base_url.rstrip("/")
    for r in requests:
        assert "rest/api/content/" in r.url
        assert r.source_id.startswith(f"{base}/pages/viewpage.action?pageId=")


def test_multi_space_source_single_key_matches_single(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`MultiSpace(space_keys=[k])` ≡ `Space(space_key=k)` по source_ids.

    Контракт композиции: один-в-списке = single-space; multi-space — это
    только chain поверх per-key источников.
    """
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    single = ConfluenceSpaceRequestSource(
        conn=confluence_connection,
        space_key=test_cfg.confluence_space_key,
        body_format=confluence_connection.body_format,
    )
    multi = ConfluenceMultiSpaceRequestSource(
        conn=confluence_connection,
        space_keys=[test_cfg.confluence_space_key],
        body_format=confluence_connection.body_format,
    )
    single_ids = [r.source_id for r in single.stream(pipeline_ctx)]
    multi_ids = [r.source_id for r in multi.stream(pipeline_ctx)]
    assert single_ids
    assert multi_ids == single_ids


def test_multi_space_source_concatenates_two_keys(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`MultiSpace([k, k])` ⇒ 2× source_ids одного space — проверка chain'а.

    Передаём один и тот же space дважды (нет требования к второму
    реальному space-ключу в test-конфиге). Это доказывает что composite
    действительно выполняет ОБА source'а, а не дедуплицирует/сбоит.
    """
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    key = test_cfg.confluence_space_key
    multi = ConfluenceMultiSpaceRequestSource(
        conn=confluence_connection,
        space_keys=[key, key],
        body_format=confluence_connection.body_format,
    )
    ids = [r.source_id for r in multi.stream(pipeline_ctx)]
    # Пагинация двух space'ов с одинаковым key даёт ровно 2× страниц
    # (composite не дедуплицирует — это работа downstream'а).
    half = len(ids) // 2
    assert half >= 1
    assert len(ids) == 2 * half
    assert ids[:half] == ids[half:]


def test_multi_space_source_empty_keys_raises(
    confluence_connection: ConfluenceConnection,
) -> None:
    """`space_keys=[]` — ValueError на конструировании (нет no-op'а)."""
    with pytest.raises(ValueError, match="space_keys"):
        ConfluenceMultiSpaceRequestSource(
            conn=confluence_connection,
            space_keys=[],
        )


def test_cql_source_returns_pages_for_real_cql(
    pipeline_ctx: PipelineContext,
    confluence_connection: ConfluenceConnection,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальный CQL-search: возвращает ≥1 страницу + правильные HttpRequest'ы.

    Подразумевается, что `test.kb.confluence_cql` написан так, что он
    действительно матчит ≥1 страницу (иначе тест провалится — это
    feature, чтобы кривой CQL не был тихим no-op).
    """
    if not test_cfg.confluence_cql:
        pytest.skip("test.kb.confluence_cql пусто")

    src = ConfluenceCqlRequestSource(
        conn=confluence_connection,
        cql=test_cfg.confluence_cql,
        body_format=confluence_connection.body_format,
    )
    requests = list(src.stream(pipeline_ctx))
    assert requests, f"cql {test_cfg.confluence_cql!r} вернул 0 страниц"
    for r in requests:
        assert "rest/api/content/" in r.url
        assert "/pages/viewpage.action?pageId=" in r.source_id
