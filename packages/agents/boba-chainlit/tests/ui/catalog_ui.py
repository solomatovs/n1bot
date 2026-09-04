"""Общее для браузерных тестов страницы каталога: селекторы холста, вход в
JSON API стенда от имени учётки, сеятель процесса над собственным источником
и его снос.

Опубликованный каталог стенда один на все модули, поэтому модуль, который
публикует своё, обязан на выходе опубликовать удаление (ProcessSeed.cleanup).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import httpx

from boba.db.clickhouse.snapshot_sample import ChSample
from boba.db.postgres.snapshot_sample import PgSample
from boba.stand.ui.stand import StandProcess


class Selector(StrEnum):
    """Селекторы холста страницы каталога."""

    READY = '[data-testid="canvas"][data-ready="true"]'
    NODE = '[data-testid="catalog-node"]'
    LANE = '[data-testid="layer-lane"]'
    EDGE_LABEL = '[data-testid="flow-edge-label"]'
    PAGE = '[data-testid="catalog-page"]'


def api_client(stand: StandProcess, login: str) -> httpx.Client:
    credential = stand.config.credential(login)
    response = httpx.post(
        f"{stand.config.base_url}/login",
        data={"username": credential.login, "password": credential.password},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"login failed: {response.status_code} {response.text[:200]}"
        )

    return httpx.Client(
        base_url=stand.config.base_url, cookies=response.cookies, timeout=30.0
    )


def ok(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        request = f"{response.request.method} {response.request.url}"
        raise RuntimeError(f"{request}: {response.status_code} {response.text[:300]}")

    return response.json()


class Ed(StrEnum):
    """Имена посеянных сущностей модуля правок: всё с префиксом ed_."""

    PREFIX = "ed_"
    SOURCE = "ed_prod"
    SRC = "ed_src"
    DST = "ed_dst"
    ORDERS = "ed_orders"
    SALES = "ed_sales"
    RETURNS = "ed_returns"
    EVENTS = "ed_events"
    ARCHIVE = "ed_archive"
    LOADER = "ed_loader"
    FULL = "ed_full"
    HASH = "ed_hash"
    HASH_FIELD = "hash_columns"
    TYPED = "ed_typed"
    TYPED_INT = "batch"
    TYPED_BOOL = "full_refresh"
    TYPED_TEXT = "note"
    TYPED_COLUMN = "key_column"
    TYPED_ROUTINE = "implemented_by"


class Objects:
    """Снимок Postgres для стендов страницы: таблицы prod/public с одними и
    теми же колонками (id — первичный ключ, name, updated_at) и процедуры
    prod/etl без аргументов."""

    DATABASE: ClassVar[str] = "prod"
    SCHEMA: ClassVar[str] = "public"
    ETL: ClassVar[str] = "etl"
    COLUMNS: ClassVar[tuple[str, ...]] = ("id", "name", "updated_at")

    @classmethod
    def table_path(cls, name: str) -> list[str]:
        return [cls.DATABASE, cls.SCHEMA, name]

    @classmethod
    def routine_path(cls, name: str) -> list[str]:
        return [cls.DATABASE, cls.ETL, name, ""]

    @classmethod
    def snapshot(
        cls, tables: Sequence[str], routines: Sequence[str] = ()
    ) -> dict[str, Any]:
        relations: list[dict[str, Any]] = []
        columns: list[dict[str, Any]] = []
        constraints: list[dict[str, Any]] = []
        for table in tables:
            relations.append(
                {
                    "database": cls.DATABASE,
                    "schema_name": cls.SCHEMA,
                    "name": table,
                    "kind": "table",
                    "owner": "app",
                }
            )
            for ordinal, column in enumerate(cls.COLUMNS, start=1):
                columns.append(
                    {
                        "database": cls.DATABASE,
                        "schema_name": cls.SCHEMA,
                        "relation": table,
                        "name": column,
                        "ordinal": ordinal,
                        "type": "text",
                        "nullable": ordinal > 1,
                    }
                )

            constraints.append(
                {
                    "database": cls.DATABASE,
                    "schema_name": cls.SCHEMA,
                    "relation": table,
                    "name": f"{table}_pkey",
                    "kind": "primary",
                    "columns": ["id"],
                    "definition": "PRIMARY KEY (id)",
                }
            )

        procedures: list[dict[str, Any]] = []
        for routine in routines:
            procedures.append(
                {
                    "database": cls.DATABASE,
                    "schema_name": cls.ETL,
                    "name": routine,
                    "signature": "",
                    "kind": "procedure",
                    "language": "plpgsql",
                    "body": "BEGIN END",
                    "definition": f"CREATE PROCEDURE {cls.ETL}.{routine}() ...",
                }
            )

        return {
            "kind": "postgres",
            "databases": [{"name": cls.DATABASE}],
            "schemas": [
                {"database": cls.DATABASE, "name": cls.SCHEMA},
                {"database": cls.DATABASE, "name": cls.ETL},
            ],
            "relations": relations,
            "columns": columns,
            "constraints": constraints,
            "routines": procedures,
        }


@dataclass(frozen=True)
class FlowSpec:
    """Поток сида: узлы по именам, вид загрузки, значения полей."""

    source: str
    target: str
    kind: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessSpec:
    """Что сеет ProcessSeed: имя источника, слои по порядку, таблицы и
    процедуры по слоям, запасные таблицы источника вне процесса, виды
    загрузки, потоки."""

    source_name: str
    layers: tuple[str, ...]
    tables: Mapping[str, str]
    routines: Mapping[str, str] = field(default_factory=dict)
    spare_tables: tuple[str, ...] = ()
    kinds: tuple[dict[str, Any], ...] = ()
    flows: tuple[FlowSpec, ...] = ()
    id_base: int = 0xE000


class ProcessSeed:
    """Процесс модуля над собственным источником: источник с таблицами и
    процедурами, слои, узлы по одному на объект, виды загрузки, потоки.
    Публикуется одной версией; cleanup публикует удаление всего своего и
    удаляет источник, чтобы соседние модули видели прежний каталог."""

    def __init__(self, api: Api, spec: ProcessSpec) -> None:
        self.api = api
        self.spec = spec
        self.source_name = spec.source_name
        self.layers = spec.layers
        self.tables = dict(spec.tables)
        self.routines = dict(spec.routines)
        self.kinds = list(spec.kinds)
        self.flows = list(spec.flows)
        self.id_base = spec.id_base
        self.ids: dict[str, str] = {}
        self.source_id = api.create_source("postgres", spec.source_name)
        tables = [*self.tables, *spec.spare_tables]
        snapshot = Objects.snapshot(tables, list(self.routines))
        api.write_source_version(self.source_id, snapshot)

    def id_of(self, name: str) -> str:
        if name not in self.ids:
            self.ids[name] = str(UUID(int=len(self.ids) + self.id_base))

        return self.ids[name]

    def ref(self, name: str) -> dict[str, Any]:
        if name in self.routines:
            path = Objects.routine_path(name)
            return {"source_id": self.source_id, "kind": "routine", "path": path}

        path = Objects.table_path(name)
        return {"source_id": self.source_id, "kind": "relation", "path": path}

    def address(self, name: str) -> str:
        return "/".join(self.ref(name)["path"])

    def node(self, name: str) -> str:
        """Селектор карточки узла на холсте."""
        return f'{Selector.NODE}[data-node="{self.address(name)}"]'

    def tree_object(self, name: str) -> str:
        """Селектор таблицы в дереве источника: под группой tables схемы."""
        path = f"{Objects.DATABASE}/{Objects.SCHEMA}/tables/{name}"
        return f'[data-testid="tree-node"][data-path="{path}"]'

    def next_version(self, tables: Sequence[str]) -> int:
        """Новая версия источника с другим набором таблиц: процесс над прежней
        версией устаревает."""
        snapshot = Objects.snapshot(tables, list(self.routines))
        return self.api.write_source_version(self.source_id, snapshot)

    def node_op(
        self, name: str, layer: str, alias: str | None = None
    ) -> dict[str, Any]:
        return {
            "op": "add_node",
            "node": {
                "id": self.id_of(name),
                "layer_id": self.id_of(layer),
                "ref": self.ref(name),
                "alias": alias,
                "note": "",
            },
        }

    def operations(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for position, layer in enumerate(self.layers):
            ops.append(
                {
                    "op": "add_layer",
                    "layer": {
                        "id": self.id_of(layer),
                        "name": layer,
                        "position": position,
                        "description": "",
                    },
                }
            )

        for name, layer in self.tables.items():
            ops.append(self.node_op(name, layer))

        for name, layer in self.routines.items():
            ops.append(self.node_op(name, layer))

        for kind in self.kinds:
            ops.append(
                {
                    "op": "add_load_kind",
                    "load_kind": {
                        "id": self.id_of(kind["name"]),
                        "name": kind["name"],
                        "description": "",
                        "fields": kind.get("fields", []),
                    },
                }
            )

        for flow in self.flows:
            ops.append(
                {
                    "op": "add_flow",
                    "flow": {
                        "id": self.id_of(f"{flow.source}->{flow.target}"),
                        "from_node_id": self.id_of(flow.source),
                        "to_node_id": self.id_of(flow.target),
                        "load": {
                            "kind_id": self.id_of(flow.kind),
                            "values": flow.values,
                        },
                        "description": "",
                    },
                }
            )

        return ops

    def publish(self, name: str) -> int:
        return self.api.publish_ops(name, self.operations())

    def cleanup_operations(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Снос всего своего из опубликованного снимка: потоки узлов этого
        источника, сами узлы, слои и виды с именами сида."""
        node_ids = {
            str(node["id"])
            for node in snapshot["nodes"].values()
            if node["ref"]["source_id"] == self.source_id
        }
        ops: list[dict[str, Any]] = []
        for flow in snapshot["flows"].values():
            touches = flow["from_node_id"] in node_ids
            if flow["to_node_id"] in node_ids:
                touches = True

            if touches:
                ops.append({"op": "remove_flow", "id": flow["id"]})

        for node_id in node_ids:
            ops.append({"op": "remove_node", "id": node_id})

        for layer in snapshot["layers"].values():
            if layer["name"] in self.layers:
                ops.append({"op": "remove_layer", "id": layer["id"]})

        kind_names = {kind["name"] for kind in self.kinds}
        for kind in snapshot["load_kinds"].values():
            if kind["name"] in kind_names:
                ops.append({"op": "remove_load_kind", "id": kind["id"]})

        return ops

    def cleanup(self) -> None:
        ops = self.cleanup_operations(self.api.snapshot())
        if ops:
            self.api.publish_ops(f"{self.source_name} cleanup", ops)

        self.api.delete_source(self.source_id)


class Seed(ProcessSeed):
    """Процесс модуля правок: два слоя, три таблицы и процедура, три вида
    загрузки, один поток."""

    def __init__(self, api: Api) -> None:
        super().__init__(api, self.spec_of())

    @staticmethod
    def spec_of() -> ProcessSpec:
        return ProcessSpec(
            source_name=Ed.SOURCE,
            layers=(Ed.SRC, Ed.DST),
            tables={Ed.ORDERS: Ed.SRC, Ed.SALES: Ed.DST, Ed.RETURNS: Ed.DST},
            routines={Ed.LOADER: Ed.DST},
            spare_tables=(Ed.EVENTS, Ed.ARCHIVE),
            kinds=(
                {"name": Ed.FULL, "fields": []},
                {
                    "name": Ed.HASH,
                    "fields": [
                        {
                            "name": Ed.HASH_FIELD,
                            "type": "columns",
                            "side": "source",
                            "required": True,
                            "description": "",
                        }
                    ],
                },
                {
                    "name": Ed.TYPED,
                    "fields": [
                        _field(Ed.TYPED_INT, "int", required=True),
                        _field(Ed.TYPED_BOOL, "bool"),
                        _field(Ed.TYPED_TEXT, "text"),
                        _field(Ed.TYPED_COLUMN, "column", side="target"),
                        _field(Ed.TYPED_ROUTINE, "routine"),
                    ],
                },
            ),
            flows=(FlowSpec(Ed.ORDERS, Ed.SALES, Ed.FULL),),
        )


def _field(
    name: str, kind: str, *, required: bool = False, side: str = "any"
) -> dict[str, Any]:
    return {
        "name": name,
        "type": kind,
        "side": side,
        "required": required,
        "description": "",
    }


class Api:
    """Ходы в JSON API стенда от имени администратора."""

    def __init__(self, admin: httpx.Client) -> None:
        self.admin = admin

    def new_draft(self, name: str) -> str:
        draft = ok(self.admin.post("/api/catalog/drafts", json={"name": name}))
        return str(draft["id"])

    def state(self, draft_id: str) -> dict[str, Any]:
        return ok(self.admin.get(f"/api/catalog/drafts/{draft_id}"))

    def append(self, draft_id: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        seq = self.state(draft_id)["seq"]
        return ok(
            self.admin.post(
                f"/api/catalog/drafts/{draft_id}/ops",
                json={"expected_seq": seq, "operations": ops},
            )
        )

    def publish(self, draft_id: str) -> int:
        version = ok(self.admin.post(f"/api/catalog/drafts/{draft_id}/publish"))
        return int(version["number"])

    def discard(self, draft_id: str) -> None:
        response = self.admin.delete(f"/api/catalog/drafts/{draft_id}")
        if response.status_code not in (200, 404, 409):
            msg = (
                f"DELETE /api/catalog/drafts/{draft_id}: expected 200, 404 or 409, "
                f"got {response.status_code} {response.text[:200]}"
            )
            raise RuntimeError(msg)

    def snapshot(self) -> dict[str, Any]:
        return ok(self.admin.get("/api/catalog/snapshot"))

    def publish_ops(self, name: str, ops: list[dict[str, Any]]) -> int:
        draft_id = self.new_draft(name)
        self.append(draft_id, ops)
        return self.publish(draft_id)

    def node_addresses(self) -> set[str]:
        addresses: set[str] = set()
        for node in self.snapshot()["nodes"].values():
            addresses.add("/".join(node["ref"]["path"]))

        return addresses

    def create_view(self, name: str, layer_ids: list[str], node_ids: list[str]) -> str:
        body = {"name": name, "layer_ids": layer_ids, "node_ids": node_ids}
        view = ok(self.admin.post("/api/catalog/views", json=body))
        return str(view["id"])

    def views(self) -> list[dict[str, Any]]:
        response = self.admin.get("/api/catalog/views")
        if response.status_code != 200:
            raise RuntimeError(f"views: {response.status_code} {response.text[:200]}")

        return list(response.json())

    def layout(self, view_id: str) -> dict[str, tuple[float, float]]:
        """Сохранённые позиции узлов вида по id узла."""
        layout = ok(self.admin.get(f"/api/catalog/views/{view_id}/layout"))
        positions: dict[str, tuple[float, float]] = {}
        for position in layout["positions"]:
            node_id = str(position["node_id"])
            positions[node_id] = (float(position["x"]), float(position["y"]))

        return positions

    def delete_view(self, view_id: str) -> None:
        response = self.admin.delete(f"/api/catalog/views/{view_id}")
        if response.status_code not in (200, 404):
            msg = (
                f"DELETE /api/catalog/views/{view_id}: expected 200 or 404, "
                f"got {response.status_code} {response.text[:200]}"
            )
            raise RuntimeError(msg)

    # --- источники ---

    def create_source(
        self, kind: str, name: str, *, manual: bool = False, description: str = ""
    ) -> str:
        body = {
            "kind": kind,
            "name": name,
            "manual": manual,
            "description": description,
        }
        return str(ok(self.admin.post("/api/catalog/sources", json=body))["id"])

    def write_source_version(self, source_id: str, snapshot: dict[str, Any]) -> int:
        version = ok(
            self.admin.post(
                f"/api/catalog/sources/{source_id}/versions",
                json={"snapshot": snapshot},
            )
        )
        return int(version["version"])

    def sources(self) -> list[dict[str, Any]]:
        return list(ok_list(self.admin.get("/api/catalog/sources")))

    def delete_source(self, source_id: str) -> None:
        response = self.admin.delete(f"/api/catalog/sources/{source_id}")
        if response.status_code not in (200, 404):
            msg = (
                f"DELETE /api/catalog/sources/{source_id}: expected 200 or 404, "
                f"got {response.status_code} {response.text[:200]}"
            )
            raise RuntimeError(msg)

    def source_draft_state(self, draft_id: str) -> dict[str, Any]:
        return ok(self.admin.get(f"/api/catalog/source-drafts/{draft_id}"))


def ok_list(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code != 200:
        request = f"{response.request.method} {response.request.url}"
        raise RuntimeError(f"{request}: {response.status_code} {response.text[:300]}")

    return list(response.json())


class SourceSeed:
    """Три источника из образцов домена: prod (postgres, v1 и v2), dwh
    (clickhouse, v1), planned (postgres, ручной, без версий)."""

    PROD: ClassVar[str] = "src_prod"
    DWH: ClassVar[str] = "src_dwh"
    PLANNED: ClassVar[str] = "src_planned"

    def __init__(self, api: Api) -> None:
        self.api = api
        pg = PgSample()
        ch = ChSample()
        self.prod = api.create_source(
            "postgres", self.PROD, description="Prod database"
        )
        api.write_source_version(self.prod, pg.snapshot().model_dump(mode="json"))
        api.write_source_version(self.prod, pg.next_version().model_dump(mode="json"))
        self.dwh = api.create_source("clickhouse", self.DWH)
        api.write_source_version(self.dwh, ch.snapshot().model_dump(mode="json"))
        self.planned = api.create_source("postgres", self.PLANNED, manual=True)

    def cleanup(self) -> None:
        for source in self.api.sources():
            if str(source["name"]).startswith("src_"):
                self.api.delete_source(str(source["id"]))
