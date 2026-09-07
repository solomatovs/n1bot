"""Общее для браузерных тестов страницы каталога: селекторы холста, вход в
JSON API стенда от имени учётки, сеятель процесса над собственным
подключением и его снос.

Каталог стенда один на все модули, поэтому модуль на выходе удаляет свой
процесс, забывает версии своего подключения и снимает само подключение
(ProcessSeed.cleanup).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import httpx
from playwright.sync_api import FloatRect, Locator, Page

from boba.db.clickhouse.snapshot_sample import ChSample
from boba.db.postgres.snapshot_sample import PgSample
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.stand import StandProcess


class Selector(StrEnum):
    """Селекторы холста страницы каталога."""

    READY = '[data-testid="canvas"][data-ready="true"]'
    NODE = '[data-testid="catalog-node"]'
    FRAME = '[data-testid="group-frame"]'
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


def settled_box(page: Page, target: Locator) -> FloatRect:
    """Рамка элемента холста после того, как раскладка перестала двигаться:
    клик или перетаскивание по ещё едущему ребру или ручке промахивается."""
    previous = target.bounding_box()
    for _ in range(50):
        page.wait_for_timeout(100)
        current = target.bounding_box()
        if current is not None and current == previous:
            return current
        previous = current

    raise AssertionError("canvas layout did not settle in 5 s")


def ok(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        request = f"{response.request.method} {response.request.url}"
        raise RuntimeError(f"{request}: {response.status_code} {response.text[:300]}")

    return response.json()


class Ed(StrEnum):
    """Имена посеянных сущностей модуля правок: всё с префиксом ed_."""

    PREFIX = "ed_"
    PROCESS = "ed_process"
    CONNECTION = "ed_prod"
    SRC = "ed_src"
    DST = "ed_dst"
    ORDERS = "ed_orders"
    SALES = "ed_sales"
    RETURNS = "ed_returns"
    EVENTS = "ed_events"
    ARCHIVE = "ed_archive"
    LOADER = "ed_loader"


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
        indexes: list[dict[str, Any]] = []
        # каждая таблица кроме первой ссылается внешним ключом на первую
        parent = tables[0]
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
            indexes.append(
                {
                    "database": cls.DATABASE,
                    "schema_name": cls.SCHEMA,
                    "relation": table,
                    "name": f"{table}_name_idx",
                    "method": "btree",
                    "columns": ["name"],
                    "definition": f"CREATE INDEX {table}_name_idx ON {table} (name)",
                }
            )
            if table == parent:
                continue

            constraints.append(
                {
                    "database": cls.DATABASE,
                    "schema_name": cls.SCHEMA,
                    "relation": table,
                    "name": f"{table}_{parent}_fk",
                    "kind": "foreign",
                    "columns": ["id"],
                    "ref_schema": cls.SCHEMA,
                    "ref_relation": parent,
                    "ref_columns": ["id"],
                    "on_update": "NO ACTION",
                    "on_delete": "CASCADE",
                    "definition": f"FOREIGN KEY (id) REFERENCES {parent}(id)",
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
            "indexes": indexes,
            "routines": procedures,
        }


@dataclass(frozen=True)
class FlowSpec:
    """Поток сида: узлы по именам, пары колонок «источник → приёмник» и
    описание — оно же ярлык линии на холсте (без описания ярлыка нет)."""

    source: str
    target: str
    columns: tuple[tuple[str, str], ...] = ()
    description: str = ""


@dataclass(frozen=True)
class ProcessSpec:
    """Что сеет ProcessSeed: имя процесса и подключения, группы, таблицы и
    процедуры по группам (пустая строка — вне групп), запасные таблицы
    подключения вне процесса, потоки. Позиции карточек — сеткой: колонка по
    группе, строка по порядку в ней."""

    process_name: str
    connection_name: str
    groups: tuple[str, ...]
    tables: Mapping[str, str]
    routines: Mapping[str, str] = field(default_factory=dict)
    spare_tables: tuple[str, ...] = ()
    flows: tuple[FlowSpec, ...] = ()
    id_base: int = 0xE000


class ProcessSeed:
    """Процесс модуля над собственным подключением: подключение стенда с
    версией снимка из таблиц и процедур, свой процесс с группами, узлами по
    одному на объект с позициями и потоками. Публикуется одной версией; cleanup удаляет
    процесс, забывает версии и снимает подключение, чтобы соседние модули
    видели прежний каталог."""

    def __init__(self, api: Api, spec: ProcessSpec) -> None:
        self.api = api
        self.spec = spec
        self.process_name = spec.process_name
        self.connection_name = spec.connection_name
        self.groups = spec.groups
        self.tables = dict(spec.tables)
        self.routines = dict(spec.routines)
        self.flows = list(spec.flows)
        self.id_base = spec.id_base
        self.ids: dict[str, str] = {}
        self.connection_id = api.add_connection(spec.connection_name, "postgres")
        tables = [*self.tables, *spec.spare_tables]
        snapshot = Objects.snapshot(tables, list(self.routines))
        api.write_connection_version(self.connection_id, snapshot)
        self.process_id = api.create_process(spec.process_name)

    def id_of(self, name: str) -> str:
        if name not in self.ids:
            self.ids[name] = str(UUID(int=len(self.ids) + self.id_base))

        return self.ids[name]

    def ref(self, name: str) -> dict[str, Any]:
        if name in self.routines:
            path = Objects.routine_path(name)
            return {
                "connection_id": self.connection_id,
                "kind": "routine",
                "path": path,
            }

        path = Objects.table_path(name)
        return {"connection_id": self.connection_id, "kind": "relation", "path": path}

    def address(self, name: str) -> str:
        return "/".join(self.ref(name)["path"])

    def node(self, name: str) -> str:
        """Селектор карточки узла на холсте."""
        return f'{Selector.NODE}[data-node="{self.address(name)}"]'

    def tree_object(self, name: str) -> str:
        """Селектор таблицы в дереве подключения: под группой tables схемы."""
        path = f"{Objects.DATABASE}/{Objects.SCHEMA}/tables/{name}"
        return f'[data-testid="tree-node"][data-path="{path}"]'

    def next_version(self, tables: Sequence[str]) -> int:
        """Новая версия снимка с другим набором таблиц: процесс над прежней
        версией устаревает."""
        snapshot = Objects.snapshot(tables, list(self.routines))
        return self.api.write_connection_version(self.connection_id, snapshot)

    COLUMN_STEP: ClassVar[int] = 420
    ROW_STEP: ClassVar[int] = 240

    def position_of(self, name: str) -> dict[str, float]:
        """Место карточки: колонка по группе (вне групп — последняя), строка по
        порядку объекта среди объектов той же группы."""
        members = {**self.tables, **self.routines}
        # запасная таблица не в процессе: колонка вне групп, первая строка
        group = members.get(name, "")
        column = len(self.groups)
        if group in self.groups:
            column = self.groups.index(group)

        row = 0
        for other, other_group in members.items():
            if other == name:
                break

            if other_group == group:
                row += 1

        return {"x": float(column * self.COLUMN_STEP), "y": float(row * self.ROW_STEP)}

    def flow_id(self, flow: FlowSpec | int) -> str:
        """Id посеянного потока: по спецификации либо по её номеру."""
        if isinstance(flow, int):
            flow = self.flows[flow]

        return self.id_of(f"{flow.source}->{flow.target}")

    def node_op(
        self, name: str, group: str, alias: str | None = None
    ) -> dict[str, Any]:
        group_id = None
        if group != "":
            group_id = self.id_of(group)

        return {
            "op": "add_node",
            "node": {
                "id": self.id_of(name),
                "ref": self.ref(name),
                "position": self.position_of(name),
                "group_id": group_id,
                "alias": alias,
                "note": "",
            },
        }

    def operations(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for group in self.groups:
            ops.append(
                {"op": "add_group", "group": {"id": self.id_of(group), "name": group}}
            )

        for name, group in self.tables.items():
            ops.append(self.node_op(name, group))

        for name, group in self.routines.items():
            ops.append(self.node_op(name, group))

        for flow in self.flows:
            links: list[dict[str, str]] = []
            for from_column, to_column in flow.columns:
                links.append({"from_column": from_column, "to_column": to_column})

            ops.append(
                {
                    "op": "add_flow",
                    "flow": {
                        "id": self.flow_id(flow),
                        "from_node_id": self.id_of(flow.source),
                        "to_node_id": self.id_of(flow.target),
                        "columns": links,
                        "description": flow.description,
                    },
                }
            )

        return ops

    def publish(self, name: str) -> int:
        return self.api.publish_ops(self.process_id, name, self.operations())

    def cleanup(self) -> None:
        """Процесс со всем содержимым, версии подключения, само подключение."""
        self.api.delete_process(self.process_id)
        self.api.forget_versions(self.connection_id)
        self.api.stand_db.remove_connections(self.connection_name)


class Seed(ProcessSeed):
    """Процесс модуля правок: две группы, три таблицы и процедура вне групп,
    один поток с двумя парами колонок."""

    def __init__(self, api: Api) -> None:
        super().__init__(api, self.spec_of())

    @staticmethod
    def spec_of() -> ProcessSpec:
        return ProcessSpec(
            process_name=Ed.PROCESS,
            connection_name=Ed.CONNECTION,
            groups=(Ed.SRC, Ed.DST),
            tables={Ed.ORDERS: Ed.SRC, Ed.SALES: Ed.DST, Ed.RETURNS: Ed.DST},
            routines={Ed.LOADER: ""},
            spare_tables=(Ed.EVENTS, Ed.ARCHIVE),
            flows=(
                FlowSpec(
                    Ed.ORDERS,
                    Ed.SALES,
                    (("id", "id"), ("name", "name")),
                    "orders to sales",
                ),
            ),
        )


class Api:
    """Ходы в JSON API стенда от имени администратора: процессы с черновиками,
    подключения стенда со снимками, ссылки на просмотр."""

    def __init__(self, admin: httpx.Client, stand_db: StandDatabase) -> None:
        self.admin = admin
        self.stand_db = stand_db

    # --- процессы ---

    def create_process(self, name: str, description: str = "") -> str:
        body = {"name": name, "description": description}
        return str(ok(self.admin.post("/api/catalog/processes", json=body))["id"])

    def processes(self) -> list[dict[str, Any]]:
        return list(ok_list(self.admin.get("/api/catalog/processes")))

    def process_id_of(self, name: str) -> str:
        for process in self.processes():
            if process["name"] == name:
                return str(process["id"])

        raise AssertionError(f"process {name!r} is not in the catalog")

    def delete_process(self, process_id: str) -> None:
        response = self.admin.delete(f"/api/catalog/processes/{process_id}")
        if response.status_code not in (200, 404):
            msg = (
                f"DELETE /api/catalog/processes/{process_id}: expected 200 or 404, "
                f"got {response.status_code} {response.text[:200]}"
            )
            raise RuntimeError(msg)

    def snapshot(self, process_id: str) -> dict[str, Any]:
        return ok(self.admin.get(f"/api/catalog/processes/{process_id}/snapshot"))

    def node_addresses(self, process_id: str) -> set[str]:
        addresses: set[str] = set()
        for node in self.snapshot(process_id)["nodes"].values():
            addresses.add("/".join(node["ref"]["path"]))

        return addresses

    def share(self, process_id: str) -> str:
        share = ok(self.admin.post(f"/api/catalog/processes/{process_id}/shares"))
        return str(share["token"])

    # --- черновики ---

    def new_draft(self, process_id: str | None, name: str) -> str:
        """Черновик процесса; None — черновик нового процесса."""
        draft = ok(
            self.admin.post(
                "/api/catalog/drafts", json={"process_id": process_id, "name": name}
            )
        )
        return str(draft["id"])

    def my_drafts(self) -> list[dict[str, Any]]:
        return list(ok_list(self.admin.get("/api/catalog/drafts")))

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

    def publish_ops(self, process_id: str, name: str, ops: list[dict[str, Any]]) -> int:
        draft_id = self.new_draft(process_id, name)
        self.append(draft_id, ops)
        return self.publish(draft_id)

    # --- подключения ---

    def add_connection(self, name: str, kind: str) -> str:
        """Подключение стенда в базе брокера, видимое админу."""
        return str(self.stand_db.add_connection(name, kind))

    def connections(self) -> list[dict[str, Any]]:
        return list(ok_list(self.admin.get("/api/catalog/connections")))

    def connection_id_of(self, name: str) -> str:
        for connection in self.connections():
            if connection["name"] == name:
                return str(connection["id"])

        raise AssertionError(f"connection {name!r} is not visible")

    def synced(self) -> list[dict[str, Any]]:
        return list(ok_list(self.admin.get("/api/catalog/synced")))

    def write_connection_version(
        self, connection_id: str, snapshot: dict[str, Any]
    ) -> int:
        version = ok(
            self.admin.post(
                f"/api/catalog/connections/{connection_id}/versions",
                json={"snapshot": snapshot},
            )
        )
        return int(version["version"])

    def forget_versions(self, connection_id: str) -> None:
        """Версии снимка подключения; отсутствие версий не ошибка."""
        response = self.admin.delete(
            f"/api/catalog/connections/{connection_id}/versions"
        )
        if response.status_code not in (200, 404):
            msg = (
                f"DELETE /api/catalog/connections/{connection_id}/versions: "
                f"expected 200 or 404, got {response.status_code} "
                f"{response.text[:200]}"
            )
            raise RuntimeError(msg)


def ok_list(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code != 200:
        request = f"{response.request.method} {response.request.url}"
        raise RuntimeError(f"{request}: {response.status_code} {response.text[:300]}")

    return list(response.json())


class ConnectionSeed:
    """Три подключения стенда из образцов домена: prod (postgres, снимки v1
    и v2), dwh (clickhouse, v1), empty (postgres, без версий)."""

    PROD: ClassVar[str] = "src_prod"
    DWH: ClassVar[str] = "src_dwh"
    EMPTY: ClassVar[str] = "src_empty"
    PREFIX: ClassVar[str] = "src_"

    def __init__(self, api: Api) -> None:
        self.api = api
        pg = PgSample()
        ch = ChSample()
        self.prod = api.add_connection(self.PROD, "postgres")
        api.write_connection_version(self.prod, pg.snapshot().model_dump(mode="json"))
        api.write_connection_version(
            self.prod, pg.next_version().model_dump(mode="json")
        )
        self.dwh = api.add_connection(self.DWH, "clickhouse")
        api.write_connection_version(self.dwh, ch.snapshot().model_dump(mode="json"))
        self.empty = api.add_connection(self.EMPTY, "postgres")

    def cleanup(self) -> None:
        for synced in self.api.synced():
            if str(synced["name"]).startswith(self.PREFIX):
                self.api.forget_versions(str(synced["connection_id"]))

        self.api.stand_db.remove_connections(self.PREFIX)
