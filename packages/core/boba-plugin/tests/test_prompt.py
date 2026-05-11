"""PromptOverlay: apply + материализация через schema_from_dataclass."""

from __future__ import annotations

from dataclasses import dataclass

from boba.config.bundle import ConfigBundle, FlatConfigMaterializer
from boba.config.path import ConfigPath
from boba.config.source.dict import DictSource
from boba.plugin.prompt import PromptOverlay
from boba.schema import schema_from_dataclass
from boba.schema.coercion import ChainCoercer, NonEmpty, ParseInt, ParseString, Required
from boba.schema.declaration import (
    CollectionField,
    FieldSpec,
    KeyedShape,
    NestedField,
    ObjectSchema,
)
from boba.schema.value import StringValue

_OVERLAY_SCHEMA = schema_from_dataclass(PromptOverlay)


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


# schema_from_dataclass(PromptOverlay): структура


def test_overlay_schema_factory_is_prompt_overlay():
    assert _OVERLAY_SCHEMA.factory is PromptOverlay


def test_overlay_schema_has_two_fields():
    names = {f.name for f in _OVERLAY_SCHEMA.fields}
    assert names == {"description", "fields"}


def test_overlay_schema_description_field_is_scalar_with_text():
    desc = next(f for f in _OVERLAY_SCHEMA.fields if f.name == "description")
    assert isinstance(desc, FieldSpec)
    assert "Override общего описания" in desc.description


def test_overlay_schema_fields_field_is_keyed_collection_with_text():
    fields = next(f for f in _OVERLAY_SCHEMA.fields if f.name == "fields")
    assert isinstance(fields, CollectionField)
    assert isinstance(fields.shape, KeyedShape)
    assert "Per-field overrides" in fields.description


# materializer через автогенную схему


def test_materialize_empty_overlay_uses_defaults():
    flat = ConfigBundle.from_sources([DictSource({})]).flat
    overlay = FlatConfigMaterializer(_OVERLAY_SCHEMA).materialize(
        flat, ConfigPath.parse("tool.search.prompt"),
    )
    assert overlay == PromptOverlay(description="", fields={})


def test_materialize_with_description_only():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.search.prompt.description"): StringValue(
                        "Custom search.",
                    ),
                },
            ),
        ],
    ).flat
    overlay = FlatConfigMaterializer(_OVERLAY_SCHEMA).materialize(
        flat, ConfigPath.parse("tool.search.prompt"),
    )
    assert overlay.description == "Custom search."
    assert overlay.fields == {}


def test_materialize_with_per_field_overrides():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.search.prompt.fields.query"): StringValue(
                        "Q.",
                    ),
                    ConfigPath.parse("tool.search.prompt.fields.limit"): StringValue(
                        "L.",
                    ),
                },
            ),
        ],
    ).flat
    overlay = FlatConfigMaterializer(_OVERLAY_SCHEMA).materialize(
        flat, ConfigPath.parse("tool.search.prompt"),
    )
    assert overlay.description == ""
    assert overlay.fields == {"query": "Q.", "limit": "L."}


# Сценарий: плагин-DTO с per-tool PromptOverlay автогенерится целиком


def test_prompt_overlay_in_plugin_dto_e2e():
    @dataclass(frozen=True)
    class _PluginCfg:
        confluence_search: PromptOverlay
        confluence_page: PromptOverlay

    plugin_schema = schema_from_dataclass(_PluginCfg)
    nested_names = {
        f.name for f in plugin_schema.fields if isinstance(f, NestedField)
    }
    assert nested_names == {"confluence_search", "confluence_page"}

    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse(
                        "tool.confluence.confluence_search.description",
                    ): StringValue("Поиск только по своим."),
                    ConfigPath.parse(
                        "tool.confluence.confluence_search.fields.query",
                    ): StringValue("Поисковый запрос."),
                    ConfigPath.parse(
                        "tool.confluence.confluence_page.description",
                    ): StringValue("Outline страницы."),
                },
            ),
        ],
    ).flat
    cfg = FlatConfigMaterializer(plugin_schema).materialize(
        flat, ConfigPath.parse("tool.confluence"),
    )
    assert cfg.confluence_search.description == "Поиск только по своим."
    assert cfg.confluence_search.fields == {"query": "Поисковый запрос."}
    assert cfg.confluence_page.description == "Outline страницы."
    assert cfg.confluence_page.fields == {}

    applied = cfg.confluence_search.apply(_CANONICAL)
    assert applied.description == "Поиск только по своим."
    assert (
        next(f for f in applied.fields if f.name == "query").description
        == "Поисковый запрос."
    )
    assert (
        next(f for f in applied.fields if f.name == "limit").description
        == "Максимум hits."
    )
