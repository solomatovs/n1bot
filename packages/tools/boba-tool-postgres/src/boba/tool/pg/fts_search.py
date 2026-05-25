"""Tool fts_search: FTS-поиск по whitelist-таблице."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.pg.fts_executor import (
    FtsExecutorConfig,
    FtsQueryError,
    PgFtsExecutor,
)
from boba.tools import FromConfig, tool

__all__ = ["FtsSearchConfig", "fts_search"]


class FtsSearchConfig(BobaFlatSettings):
    """Конфиг tool fts_search (секция [tool.pg.fts_search])."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.pg.fts_search",
        defaults_from=("tool.pg",),
    )

    executor: FtsExecutorConfig
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k для fts_search.",
    )


@tool
def fts_search(
    cfg: Annotated[FtsSearchConfig, FromConfig()],
    target: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя подключения"
            ),
        ),
    ],
    query: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Поисковый запрос. Поддерживается websearch-синтаксис: "
                'кавычки для фраз ("exact phrase"), OR для альтернатив, '
                "минус-слово для исключения."
            ),
        ),
    ],
    top_k: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько hits вернуть. По умолчанию 5"
            ),
        ),
    ] = 5,
) -> list[dict[str, Any]]:
    """FTS-поиск на профиле target. Возвращает hits {id, score, metadata, snippet}."""
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    executor = PgFtsExecutor(cfg=cfg.executor)
    try:
        hits = executor.search(query=query, target=target, top_k=top_k)
    except FtsQueryError as e:
        raise RuntimeError(str(e)) from e

    return [
        {
            "id": h.id,
            "score": h.score,
            "metadata": dict(h.metadata),
            "snippet": h.snippet,
        }
        for h in hits
    ]
