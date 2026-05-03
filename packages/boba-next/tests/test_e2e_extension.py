"""End-to-end use-case: extension как самостоятельная единица конфига.

Демонстрирует целевой сценарий новой архитектуры:
  - Extension объявляет dataclass + ObjectSchema со своими полями + enabled +
    динамической секцией tools (через CollectionField + KeyedShape + ObjectItem).
  - Tool overlay (description tool'а и параметров) живёт прямо в TOML под
    [ext.<name>.tools.<id>].
  - Extension сам решает, какие tools включены и с какими descriptions.
  - Источники: TOML (низкий приоритет) + env (выше) + cli (выше всех).
    Любое значение можно переопределить через env/cli без изменения файлов.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from boba_next import (
    ChainConverter,
    CollectionField,
    ConfigBundle,
    ConfigBundleFactory,
    ConfigPath,
    Default,
    FieldSpec,
    KeyedShape,
    MaxValue,
    MinValue,
    NonEmpty,
    ObjectItem,
    ObjectSchema,
    ParseBool,
    ParseInt,
    ParseString,
    NotNull,
)

from boba.config.cli.next import CliSource
from boba.config.env.next import EnvSource
from boba.config.toml.next import TomlSource

# ─────────── Описания DTO + схем (то, что extension'у хочется выразить) ───────────


@dataclass(frozen=True)
class ParamOverlay:
    """Перекрытие описания одного параметра tool'а."""

    description: str = ""


_PARAM_OVERLAY_SCHEMA: ObjectSchema[ParamOverlay] = ObjectSchema(
    fields=[
        FieldSpec("description", ChainConverter(Default(""), ParseString())),
    ],
    factory=ParamOverlay,
)


@dataclass(frozen=True)
class ToolEntry:
    """Запись одного tool'а внутри extension'а."""

    enabled: bool
    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


_TOOL_ENTRY_SCHEMA: ObjectSchema[ToolEntry] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainConverter(Default(False), ParseBool())),
        FieldSpec("description", ChainConverter(Default(""), ParseString())),
        CollectionField(
            name="params",
            reader=ObjectItem(_PARAM_OVERLAY_SCHEMA),
            shape=KeyedShape(),
        ),
    ],
    factory=ToolEntry,
)


@dataclass(frozen=True)
class ChromadbConfig:
    enabled: bool
    persist_path: str
    max_top_k: int
    tools: Mapping[str, ToolEntry] = field(default_factory=dict)


_CHROMADB_SCHEMA: ObjectSchema[ChromadbConfig] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainConverter(Default(False), ParseBool())),
        FieldSpec(
            "persist_path",
            ChainConverter(Default(""), ParseString()),
        ),
        FieldSpec(
            "max_top_k",
            ChainConverter(Default(20), ParseInt(), MinValue(1), MaxValue(100)),
        ),
        CollectionField(
            name="tools",
            reader=ObjectItem(_TOOL_ENTRY_SCHEMA),
            shape=KeyedShape(),
        ),
    ],
    factory=ChromadbConfig,
)


# ─────────── Helpers, имитирующие то, что будет в extension'е ───────────


def select_enabled_tool_ids(cfg: ChromadbConfig) -> list[str]:
    """Возвращает имена tool'ов, которые extension должен зарегистрировать."""
    if not cfg.enabled:
        return []
    return [tool_id for tool_id, entry in cfg.tools.items() if entry.enabled]


# ─────────── Сценарии ───────────


def _bundle_from_toml(tmp_path: Path, content: str) -> ConfigBundle:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(content, encoding="utf-8")
    factory = ConfigBundleFactory()
    factory.attach_sources([TomlSource(cfg_path)])
    return factory.build()


def test_extension_disabled_yields_no_tools(tmp_path: Path):
    bundle = _bundle_from_toml(tmp_path, "")
    cfg = bundle.materialize(_CHROMADB_SCHEMA, ConfigPath.parse("$ext.chromadb"))
    assert cfg.enabled is False
    assert select_enabled_tool_ids(cfg) == []


def test_extension_enabled_but_no_tool_entries(tmp_path: Path):
    bundle = _bundle_from_toml(
        tmp_path,
        """
        [ext.chromadb]
        enabled = true
        persist_path = "./local/chroma"
        """,
    )
    cfg = bundle.materialize(_CHROMADB_SCHEMA, ConfigPath.parse("$ext.chromadb"))
    assert cfg.enabled is True
    assert cfg.persist_path == "./local/chroma"
    # Нет ни одной [ext.chromadb.tools.*] подсекции → ни один tool не включён.
    assert select_enabled_tool_ids(cfg) == []


def test_extension_with_two_tools_one_enabled(tmp_path: Path):
    bundle = _bundle_from_toml(
        tmp_path,
        """
        [ext.chromadb]
        enabled = true
        persist_path = "./local/chroma"
        max_top_k = 30

        [ext.chromadb.tools.kb_search]
        enabled = true
        description = "Поиск по векторной базе ChromaDB"

        [ext.chromadb.tools.kb_search.params.query]
        description = "Запрос на естественном языке"

        [ext.chromadb.tools.kb_search.params.top_k]
        description = "Сколько документов вернуть"

        [ext.chromadb.tools.kb_admin]
        enabled = false
        description = "Админка"
        """,
    )
    cfg = bundle.materialize(_CHROMADB_SCHEMA, ConfigPath.parse("$ext.chromadb"))

    assert cfg.enabled is True
    assert cfg.max_top_k == 30
    assert select_enabled_tool_ids(cfg) == ["kb_search"]

    kb = cfg.tools["kb_search"]
    assert kb.description == "Поиск по векторной базе ChromaDB"
    assert kb.params["query"].description == "Запрос на естественном языке"
    assert kb.params["top_k"].description == "Сколько документов вернуть"

    admin = cfg.tools["kb_admin"]
    assert admin.enabled is False
    assert admin.description == "Админка"


def test_env_overrides_toml_value(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(
        """
        [ext.chromadb]
        enabled = false
        persist_path = "./local/chroma"
        """,
        encoding="utf-8",
    )
    factory = ConfigBundleFactory()
    factory.attach_sources(
        [
            TomlSource(cfg_file),
            EnvSource({"BOBA_EXT__CHROMADB__ENABLED": "true"}),
        ]
    )
    bundle = factory.build()
    cfg = bundle.materialize(_CHROMADB_SCHEMA, ConfigPath.parse("$ext.chromadb"))
    assert cfg.enabled is True


def test_cli_overrides_env_and_toml(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(
        """
        [ext.chromadb]
        enabled = true
        max_top_k = 30
        """,
        encoding="utf-8",
    )
    factory = ConfigBundleFactory()
    factory.attach_sources(
        [
            TomlSource(cfg_file),
            EnvSource({"BOBA_EXT__CHROMADB__MAX_TOP_K": "50"}),
            CliSource(["--ext.chromadb.max_top_k=80"]),
        ]
    )
    bundle = factory.build()
    cfg = bundle.materialize(_CHROMADB_SCHEMA, ConfigPath.parse("$ext.chromadb"))
    assert cfg.max_top_k == 80


def test_subtree_returns_flat_under_prefix(tmp_path: Path):
    bundle = _bundle_from_toml(
        tmp_path,
        """
        [ext.html]
        enabled = true

        [ext.html.tools.html_outline]
        enabled = true

        [ext.chromadb]
        enabled = false
        """,
    )
    sub = bundle.flat.subtree(ConfigPath.parse("$ext.html"))
    assert ConfigPath.parse("$ext.html.enabled") in sub
    assert ConfigPath.parse("$ext.html.tools.html_outline.enabled") in sub
    assert ConfigPath.parse("$ext.chromadb.enabled") not in sub


def test_lookup_origin_traces_back_to_source(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("[a]\nb = 1\n", encoding="utf-8")
    factory = ConfigBundleFactory()
    factory.attach_sources(
        [
            TomlSource(cfg_file, name="main_toml"),
            EnvSource({"BOBA_A__C": "2"}, name="env"),
        ]
    )
    bundle = factory.build()
    assert bundle.flat.origin_of(ConfigPath.parse("$a.b")).value() == "main_toml"
    assert bundle.flat.origin_of(ConfigPath.parse("$a.c")).value() == "env"


# Re-export NotNull/NonEmpty чтобы pyright не считал их неиспользованными
# (они используются в схеме параметров, см. _PARAM_OVERLAY_SCHEMA выше).
_ = NotNull
_ = NonEmpty
