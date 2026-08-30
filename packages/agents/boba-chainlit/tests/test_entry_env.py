"""Env chainlit выставляется точкой входа до первого импорта его модулей."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from chainlit_stand import FakeSecret

from boba.chainlit.domain.keys import AppPrefix
from boba.chainlit.infra.entry import AppEntry, ChainlitEnv


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(autouse=True)
def keep_environ() -> Iterator[None]:
    """Снимок окружения на время теста.

    export_env пишет CHAINLIT_APP_ROOT прямо в os.environ, а корень здесь —
    временный каталог: без восстановления он утекал в соседние тесты, и их
    подпроцессы падали на импорте chainlit (тот заводит .files под APP_ROOT).
    """
    saved = dict(os.environ)

    yield

    os.environ.clear()
    os.environ.update(saved)


CONFIG = """
[app]
    chainlit = "${chainlit}"

[session]
    auth_secret     = "<auth_secret>"
    cookie          = "boba_token"
    cookie_samesite = "strict"
    session_ttl_sec = 3600
    session_max_sec = 86400

[chainlit]
    root        = "<root>"
    url_prefix  = "/boba"
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
        monkeypatch.delenv(ChainlitEnv.APP_ROOT, raising=False)
        monkeypatch.delenv(AppPrefix.ENV, raising=False)
        monkeypatch.delenv(ChainlitEnv.AUTH_SECRET, raising=False)

        root = tmp_path / "data"
        AppEntry.export_env(self._config(tmp_path, str(root)))

        if os.environ[ChainlitEnv.APP_ROOT] != str(root):
            raise AssertionError("os.environ[ChainlitEnv.APP_ROOT] == str(root)")
        if os.environ[AppPrefix.ENV] != "/boba":
            raise AssertionError('os.environ[AppPrefix.ENV] == "/boba"')
        if os.environ[ChainlitEnv.AUTH_SECRET] != FakeSecret.AUTH:
            raise AssertionError("os.environ[ChainlitEnv.AUTH_SECRET] == FakeSecret.…")

        assert os.environ[ChainlitEnv.COOKIE_NAME] == "boba_token"
        assert os.environ[ChainlitEnv.COOKIE_SAMESITE] == "strict"

    def test_relative_root_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        AppEntry.export_env(self._config(tmp_path, "./data"))

        if not (Path(os.environ[ChainlitEnv.APP_ROOT]).is_absolute()):
            raise AssertionError("Path(os.environ[ChainlitEnv.APP_ROOT]).is_absolute…")

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

    @pytest.mark.parametrize("module", ["boba.chainlit.infra.entry"])
    def test_module_import_leaves_cwd_clean(self, module: str, tmp_path: Path) -> None:
        result = self._probe(module, tmp_path)

        if result.stdout.strip() != "[]":
            raise AssertionError('result.stdout.strip() == "[]"')
        if (tmp_path / ".chainlit").exists():
            raise AssertionError('not (tmp_path / ".chainlit").exists()')
        if (tmp_path / ".files").exists():
            raise AssertionError('not (tmp_path / ".files").exists()')
