"""PromptOverlay: apply (description + per-field overrides)."""

from __future__ import annotations

from dataclasses import dataclass

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import ChainCoercer, NonEmpty, ParseInt, ParseString, Required
from boba.schema.declaration import (
    FieldSpec,
    ObjectSchema,
)

# Canonical (доменная) tool-схема для проверки apply


@dataclass(frozen=True)
class _SearchArgs:
    query: str
    limit: int


_CANONICAL: ObjectSchema[_SearchArgs] = ObjectSchema(
    description="Полнотекстовый поиск.",
    fields=[
        FieldSpec(
            name="query",
            coercer=ChainCoercer(Required(), ParseString(), NonEmpty()),
            description="Поисковый запрос.",
        ),
        FieldSpec(
            name="limit",
            coercer=ChainCoercer(Required(), ParseInt()),
            description="Максимум hits.",
        ),
    ],
    factory=_SearchArgs,
)


# apply


def test_apply_empty_overlay_returns_canonical_unchanged():
    overlay = PromptOverlay()
    applied = overlay.apply(_CANONICAL)
    assert applied.description == "Полнотекстовый поиск."
    assert applied.fields[0].description == "Поисковый запрос."
    assert applied.fields[1].description == "Максимум hits."


def test_apply_overrides_top_level_description():
    overlay = PromptOverlay(description="Custom search.")
    applied = overlay.apply(_CANONICAL)
    assert applied.description == "Custom search."


def test_apply_overrides_per_field_description():
    overlay = PromptOverlay(fields={"query": "Custom query."})
    applied = overlay.apply(_CANONICAL)
    query_f = next(f for f in applied.fields if f.name == "query")
    limit_f = next(f for f in applied.fields if f.name == "limit")
    assert query_f.description == "Custom query."
    assert limit_f.description == "Максимум hits."  # не тронут


def test_apply_overrides_both_top_and_fields():
    overlay = PromptOverlay(
        description="Custom search.",
        fields={"query": "Custom query.", "limit": "Custom limit."},
    )
    applied = overlay.apply(_CANONICAL)
    assert applied.description == "Custom search."
    assert (
        next(f for f in applied.fields if f.name == "query").description
        == "Custom query."
    )
    assert (
        next(f for f in applied.fields if f.name == "limit").description
        == "Custom limit."
    )


def test_apply_does_not_mutate_canonical():
    """frozen-инвариант: canonical schema остаётся неизменной после apply."""
    overlay = PromptOverlay(description="Custom.", fields={"query": "Q."})
    overlay.apply(_CANONICAL)
    assert _CANONICAL.description == "Полнотекстовый поиск."
    assert (
        next(f for f in _CANONICAL.fields if f.name == "query").description
        == "Поисковый запрос."
    )


def test_apply_empty_string_override_falls_back_to_canonical():
    overlay = PromptOverlay(description="", fields={"query": ""})
    applied = overlay.apply(_CANONICAL)
    assert applied.description == "Полнотекстовый поиск."
    assert (
        next(f for f in applied.fields if f.name == "query").description
        == "Поисковый запрос."
    )


def test_apply_unknown_field_in_overlay_is_ignored():
    overlay = PromptOverlay(fields={"unknown": "ignored"})
    applied = overlay.apply(_CANONICAL)
    names = [f.name for f in applied.fields]
    assert names == ["query", "limit"]
    assert (
        next(f for f in applied.fields if f.name == "query").description
        == "Поисковый запрос."
    )


def test_apply_preserves_coercer_chain_unchanged():
    """Overlay меняет только description; coercer-цепочка (включая Required)
    остаётся той же ссылкой — required-поведение сохраняется."""
    overlay = PromptOverlay(fields={"query": "Q."})
    applied = overlay.apply(_CANONICAL)
    query_f = next(f for f in applied.fields if f.name == "query")
    canonical_query = _CANONICAL.fields[0]
    assert isinstance(query_f, FieldSpec)
    assert isinstance(canonical_query, FieldSpec)
    assert query_f.coercer is canonical_query.coercer
