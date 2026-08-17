"""Env chainlit выставляется точкой входа до первого импорта его модулей."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FakeSecret

from boba.chainlit.infra.entry import AppEntry


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


CONFIG = """
[app]
    chainlit = "${chainlit}"

[chainlit]
    root        = "<root>"
    url_prefix  = "/boba"
    auth_secret = "<auth_secret>"
"""


class TestExportEnv:
    @staticmethod
    def _config(tmp_path: Path, root: str) -> Path:
        path = tmp_path / "config.toml"
        body = CONFIG.replace("<root>", root)
        body = body.replace("<auth_secret>", FakeSecret.AUTH)
        path.write_text(body, encoding="utf-8")
        return path

    def test_env_taken_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(AppEntry.APP_ROOT_ENV, raising=False)
        monkeypatch.delenv(AppEntry.ROOT_PATH_ENV, raising=False)
        monkeypatch.delenv(AppEntry.AUTH_SECRET_ENV, raising=False)

        root = tmp_path / "data"
        AppEntry.export_env(self._config(tmp_path, str(root)))

        if os.environ[AppEntry.APP_ROOT_ENV] != str(root):
            raise AssertionError("os.environ[AppEntry.APP_ROOT_ENV] == str(root)")
        if os.environ[AppEntry.ROOT_PATH_ENV] != "/boba":
            raise AssertionError('os.environ[AppEntry.ROOT_PATH_ENV] == "/boba"')
        if os.environ[AppEntry.AUTH_SECRET_ENV] != FakeSecret.AUTH:
            raise AssertionError("os.environ[AppEntry.AUTH_SECRET_ENV] == FakeSecret.…")

    def test_relative_root_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        AppEntry.export_env(self._config(tmp_path, "./data"))

        if not (Path(os.environ[AppEntry.APP_ROOT_ENV]).is_absolute()):
            raise AssertionError("Path(os.environ[AppEntry.APP_ROOT_ENV]).is_absolute…")

    def test_empty_root_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="root"):
            AppEntry.export_env(self._config(tmp_path, ""))


class TestEntryPointsFreeOfChainlit:
    """Точки входа не тянут chainlit на импорте: иначе app_root уедет в cwd."""

    @staticmethod
    def _probe(module: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        code = (
            f"import importlib, sys; importlib.import_module('{module}'); "
            "print([m for m in sys.modules if m.startswith('chainlit')])"
        )
        return subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )

    @pytest.mark.parametrize(
        "module", ["boba.chainlit.infra.entry", "boba.chainlit.cli.ingest"]
    )
    def test_module_import_leaves_cwd_clean(self, module: str, tmp_path: Path) -> None:
        result = self._probe(module, tmp_path)

        if result.stdout.strip() != "[]":
            raise AssertionError('result.stdout.strip() == "[]"')
        if (tmp_path / ".chainlit").exists():
            raise AssertionError('not (tmp_path / ".chainlit").exists()')
        if (tmp_path / ".files").exists():
            raise AssertionError('not (tmp_path / ".files").exists()')
