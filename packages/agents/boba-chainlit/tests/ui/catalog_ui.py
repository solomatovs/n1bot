"""Общее для браузерных тестов страницы каталога: селекторы холста, вход в
JSON API стенда от имени учётки, сид каталога с префиксом ed_ и его снос.

Опубликованный каталог стенда один на все модули, поэтому модуль, который
публикует своё, обязан на выходе опубликовать удаление (Cleanup).
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import httpx

from boba.stand.ui.stand import StandProcess


class Selector(StrEnum):
    """Селекторы холста страницы каталога."""

    READY = '[data-testid="canvas"][data-ready="true"]'
    NODE = '[data-testid="dataset-node"]'
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
    """Имена посеянных сущностей: всё с префиксом ed_, по нему же и убирается."""

    PREFIX = "ed_"
    SRC = "ed_src"
    DST = "ed_dst"
    ORDERS = "ed_orders"
    SALES = "ed_sales"
    RETURNS = "ed_returns"
    FULL = "ed_full"
    HASH = "ed_hash"
    HASH_FIELD = "hash_columns"


class Seed:
    """Каталог модуля: два слоя, три набора, два вида загрузки, один поток."""

    ID_BASE: ClassVar[int] = 0xE000
    DATASETS: ClassVar[dict[str, str]] = {
        Ed.ORDERS: Ed.SRC,
        Ed.SALES: Ed.DST,
        Ed.RETURNS: Ed.DST,
    }
    COLUMNS: ClassVar[tuple[str, ...]] = ("id", "name")

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def id_of(self, name: str) -> str:
        if name not in self.ids:
            self.ids[name] = str(UUID(int=len(self.ids) + self.ID_BASE))

        return self.ids[name]

    def operations(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for layer in (Ed.SRC, Ed.DST):
            ops.append(
                {"op": "add_layer", "layer": {"id": self.id_of(layer), "name": layer}}
            )

        for dataset, layer in self.DATASETS.items():
            ops.append(
                {
                    "op": "add_dataset",
                    "dataset": {
                        "id": self.id_of(dataset),
                        "layer_id": self.id_of(layer),
                        "name": dataset,
                    },
                }
            )
            for position, column in enumerate(self.COLUMNS):
                ops.append(
                    {
                        "op": "add_column",
                        "column": {
                            "id": self.id_of(f"{dataset}.{column}"),
                            "dataset_id": self.id_of(dataset),
                            "name": column,
                            "type": "text",
                            "nullable": position > 0,
                            "is_key": position == 0,
                            "position": position,
                        },
                    }
                )

        ops.append(
            {
                "op": "add_load_kind",
                "load_kind": {"id": self.id_of(Ed.FULL), "name": Ed.FULL, "fields": []},
            }
        )
        ops.append(
            {
                "op": "add_load_kind",
                "load_kind": {
                    "id": self.id_of(Ed.HASH),
                    "name": Ed.HASH,
                    "fields": [
                        {"name": Ed.HASH_FIELD, "type": "columns", "required": True}
                    ],
                },
            }
        )
        ops.append(
            {
                "op": "add_flow",
                "flow": {
                    "id": self.id_of("orders->sales"),
                    "from_dataset_id": self.id_of(Ed.ORDERS),
                    "to_dataset_id": self.id_of(Ed.SALES),
                    "load": {"kind_id": self.id_of(Ed.FULL), "values": {}},
                },
            }
        )
        return ops


class Cleanup:
    """Операции удаления всего с префиксом ed_ из опубликованного снимка."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot

    def _mine(self, table: str) -> Iterator[dict[str, Any]]:
        for entity in self.snapshot[table].values():
            if str(entity["name"]).startswith(Ed.PREFIX):
                yield entity

    def operations(self) -> list[dict[str, Any]]:
        dataset_ids = {entity["id"] for entity in self._mine("datasets")}
        ops: list[dict[str, Any]] = []
        for flow in self.snapshot["flows"].values():
            touches = flow["from_dataset_id"] in dataset_ids
            if flow["to_dataset_id"] in dataset_ids:
                touches = True

            if touches:
                ops.append({"op": "remove_flow", "id": flow["id"]})

        for dataset_id in dataset_ids:
            ops.append({"op": "remove_dataset", "id": dataset_id})

        for layer in self._mine("layers"):
            ops.append({"op": "remove_layer", "id": layer["id"]})

        for kind in self._mine("load_kinds"):
            ops.append({"op": "remove_load_kind", "id": kind["id"]})

        return ops


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
            raise RuntimeError(f"discard failed: {response.status_code}")

    def snapshot(self) -> dict[str, Any]:
        return ok(self.admin.get("/api/catalog/snapshot"))

    def publish_ops(self, name: str, ops: list[dict[str, Any]]) -> int:
        draft_id = self.new_draft(name)
        self.append(draft_id, ops)
        return self.publish(draft_id)

    def dataset_names(self) -> set[str]:
        names: set[str] = set()
        for dataset in self.snapshot()["datasets"].values():
            names.add(str(dataset["name"]))

        return names

    def views(self) -> list[dict[str, Any]]:
        response = self.admin.get("/api/catalog/views")
        if response.status_code != 200:
            raise RuntimeError(f"views: {response.status_code} {response.text[:200]}")

        return list(response.json())

    def layout(self, view_id: str) -> dict[str, tuple[float, float]]:
        """Сохранённые позиции узлов вида по id набора."""
        layout = ok(self.admin.get(f"/api/catalog/views/{view_id}/layout"))
        positions: dict[str, tuple[float, float]] = {}
        for position in layout["positions"]:
            dataset_id = str(position["dataset_id"])
            positions[dataset_id] = (float(position["x"]), float(position["y"]))

        return positions

    def delete_view(self, view_id: str) -> None:
        response = self.admin.delete(f"/api/catalog/views/{view_id}")
        if response.status_code not in (200, 404):
            raise RuntimeError(f"delete view failed: {response.status_code}")
