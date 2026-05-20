"""
Интеграционный тест `kb_search`: реальный PersistentClient + реальный EF.

Все параметры доступа к ChromaDB и embedding'у читаются из
`ChromadbPluginConfig` — единая система конфигурирования (`local/config.toml` +
env `BOBA_TOOL__CHROMADB__*`). Тест skip-ается, если конфиг не сконструировался
(например, не задан обязательный `persist_path`) — то есть запускается только
у оператора, у которого плагин реально настроен.

`kb_search` помечен `@tool`, но это лёгкий декоратор-маркер: он только ставит
sentinel-атрибут и возвращает ту же функцию. `Annotated[..., FromDI/FromConfig]`
читает framework на этапе `AgentBuilder.build()`, в run-time это обычная
функция — поэтому тест вызывает её напрямую, подсовывая `kb` и `cfg` руками
(без Dishka-контейнера).

Параметры самого запроса не входят в plugin-конфиг (`extra="ignore"`
запрещает посторонние ключи), поэтому передаются через отдельные env-варсы:

    BOBA_KB_SEARCH_QUERY=<строка>    # по умолчанию "test"
    BOBA_KB_SEARCH_TOP_K=<int>       # по умолчанию 5

Запуск:

    BOBA_KB_SEARCH_QUERY="как оформить возврат?" \\
        pytest packages/tools/boba-tool-chromadb/tests/test_kb_search.py -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.chromadb.embedder_factory import build_chromadb_embedding_function
from boba.tool.chromadb.kb import ChromaKnowledgeBase
from boba.tool.chromadb.kb_search import kb_search

if TYPE_CHECKING:
    # Только для type-checker'а: pytest сам резолвит фикстуру по имени;
    # импортировать `tests.conftest` напрямую в run-time нельзя — это
    # special-loaded модуль pytest'а, не пакет.
    from tests.conftest import KbSearchRunSpec


def test_kb_search_real(kb_search_run: KbSearchRunSpec | None) -> None:
    """Реальный kb_search на персистентном ChromaDB оператора.

    Печатает hits через print (требует `pytest -s`) — оператор видит, что
    реально нашлось по его запросу, без копания в логах. В конце —
    контракт-проверки формы ответа (list[dict] с ожидаемыми ключами).
    """
    if kb_search_run is None:
        pytest.skip(
            "kb_search integration disabled: set [tool.chromadb] persist_path "
            "in config or BOBA_TOOL__CHROMADB__PERSIST_PATH env",
        )

    import chromadb  # noqa: PLC0415

    cfg = kb_search_run.cfg
    client = chromadb.PersistentClient(path=cfg.persist_path)
    ef = build_chromadb_embedding_function(
        model=cfg.embedding_model,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )
    kb = ChromaKnowledgeBase(
        client=client,
        snippet_chars=cfg.snippet_chars,
        embedding_function=ef,
    )

    hits = kb_search(
        query=kb_search_run.query,
        kb=kb,
        cfg=cfg,
        top_k=kb_search_run.top_k,
    )

    _emit("")
    _emit(f"persist_path:    {cfg.persist_path}")
    _emit(f"collection:      {cfg.ingest_collection}")
    _emit(f"embedding_model: {cfg.embedding_model}")
    _emit(f"snippet_chars:   {cfg.snippet_chars}")
    _emit(f"query:           {kb_search_run.query!r}")
    _emit(f"top_k:           {kb_search_run.top_k}")
    _emit(f"hits:            {len(hits)}")
    for i, h in enumerate(hits):
        _emit(
            f"  [{i}] dist={h['distance']:.4f}  id={h['id']}  link={h['link']}",
        )
        _emit(f"       snippet: {h['snippet']}")

    assert isinstance(hits, list)
    for h in hits:
        assert {"id", "distance", "link", "metadata", "snippet"} <= h.keys()
        assert isinstance(h["distance"], float)
        assert isinstance(h["metadata"], dict)
        assert isinstance(h["snippet"], str)


def test_kb_search_top_k_ceiling(
    kb_search_run: KbSearchRunSpec | None,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError. Защита от случайно большого
    запроса, который положит KB или вернёт неподъёмный объём.

    Тоже интеграционный (использует тот же реальный cfg), но дешёвый —
    падает до похода в Chroma.
    """
    if kb_search_run is None:
        pytest.skip(
            "kb_search integration disabled: set [tool.chromadb] persist_path "
            "in config or BOBA_TOOL__CHROMADB__PERSIST_PATH env",
        )

    import chromadb  # noqa: PLC0415

    cfg = kb_search_run.cfg
    client = chromadb.PersistentClient(path=cfg.persist_path)
    ef = build_chromadb_embedding_function(
        model=cfg.embedding_model,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )
    kb = ChromaKnowledgeBase(
        client=client,
        snippet_chars=cfg.snippet_chars,
        embedding_function=ef,
    )

    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        kb_search(
            query=kb_search_run.query,
            kb=kb,
            cfg=cfg,
            top_k=cfg.max_top_k + 1,
        )


def _emit(msg: str) -> None:
    """Print hits для оператора (требует `pytest -s`)."""
    print(msg)  # noqa: T201
