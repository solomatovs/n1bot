"""Схема API собирается без хоста и без chainlit; пути и модели на месте."""

from __future__ import annotations

import subprocess
import sys

from boba.api.schema import OpenApiDocument
from boba.api.urls import ApiVersion, ToolCallUrl, WorkflowUrl


def test_schema_lists_v1_paths_and_models() -> None:
    document = OpenApiDocument.render()

    paths = set(document["paths"])
    for url in [*WorkflowUrl, ToolCallUrl.CALL]:
        if f"{ApiVersion.V1}{url}" not in paths:
            raise AssertionError((url, sorted(paths)))

    schemas = set(document["components"]["schemas"])
    for name in (
        "StoredWorkflow",
        "StoredRun",
        "RunState",
        "ToolCallReply",
        "ToolFacts",
    ):
        if name not in schemas:
            raise AssertionError((name, sorted(schemas)))


def test_api_package_does_not_import_chainlit() -> None:
    code = (
        "import sys\n"
        "import boba.api.app, boba.api.schema\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] == 'chainlit']\n"
        "raise SystemExit(1 if loaded else 0)\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )

    if result.returncode != 0:
        raise AssertionError(result.stderr)
