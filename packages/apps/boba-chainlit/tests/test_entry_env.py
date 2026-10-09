"""Env chainlit выставляется точкой входа до первого импорта его модулей."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from chainlit_stand import FakeSecret

from boba.chainlit.infra.entry import AppEntry, ChainlitEnv
from boba.runtime.config import ConfigFiles


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
    files_dir   = "<files_dir>"
    url_prefix  = "/boba"
"""

ASSETS = Path(__file__).resolve().parents[1] / "assets"
"""Ассеты пакета: готовый app_root, который отладка отдаёт chainlit напрямую."""

FILES_PROBE = """
import sys
from pathlib import Path
from types import SimpleNamespace

from boba.chainlit.infra.entry import AppEntry
from boba.runtime.config import ConfigFiles

files = ConfigFiles(config=Path(sys.argv[1]), site=Path(sys.argv[2]))
AppEntry.export_env(files)
AppEntry.attachments(files).install()

import chainlit.config
import chainlit.server
from chainlit.session import BaseSession

print(chainlit.config.FILES_DIRECTORY)
print(chainlit.server.FILES_DIRECTORY)
print(BaseSession.files_dir.fget(SimpleNamespace(id="s1")))
"""
"""Что видит chainlit после точки входа: каталог вложений модуля config, его
копия в chainlit.server и каталог сессии."""


class TestExportEnv:
    @staticmethod
    def _config(tmp_path: Path, root: str) -> ConfigFiles:
        path = tmp_path / "config.toml"
        body = CONFIG.replace("<root>", root)
        body = body.replace("<files_dir>", str(tmp_path / "files"))
        body = body.replace("<auth_secret>", FakeSecret.AUTH)
        path.write_text(body, encoding="utf-8")
        site = tmp_path / "site.toml"
        site.write_text("", encoding="utf-8")

        return ConfigFiles(config=path, site=site)

    def test_env_taken_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ChainlitEnv.APP_ROOT, raising=False)
        monkeypatch.delenv(ChainlitEnv.ROOT_PATH, raising=False)
        monkeypatch.delenv(ChainlitEnv.AUTH_SECRET, raising=False)

        root = tmp_path / "data"
        AppEntry.export_env(self._config(tmp_path, str(root)))

        if os.environ[ChainlitEnv.APP_ROOT] != str(root):
            raise AssertionError("os.environ[ChainlitEnv.APP_ROOT] == str(root)")
        if os.environ[ChainlitEnv.ROOT_PATH] != "/boba":
            raise AssertionError('os.environ[ChainlitEnv.ROOT_PATH] == "/boba"')
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
        return subprocess.run(
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


class TestChainlitFilesOutsideAppRoot:
    """chainlit пишет вложения в [chainlit].files_dir, а app_root не трогает.

    Проверка идёт в подпроцессе: путь chainlit фиксирует на импорте. Упадёт,
    если после обновления chainlit запись снова пойдёт в APP_ROOT.
    """

    @staticmethod
    def _tree(root: Path) -> list[tuple[str, int, int]]:
        entries: list[tuple[str, int, int]] = []
        for path in sorted(root.rglob("*")):
            stat = path.stat()
            entries.append(
                (str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size)
            )

        root_stat = root.stat()
        entries.append((".", root_stat.st_mtime_ns, 0))

        return entries

    @staticmethod
    def _config(tmp_path: Path, root: Path, files_dir: str) -> ConfigFiles:
        path = tmp_path / "config.toml"
        body = CONFIG.replace("<root>", str(root))
        body = body.replace("<files_dir>", files_dir)
        body = body.replace("<auth_secret>", FakeSecret.AUTH)
        path.write_text(body, encoding="utf-8")
        site = tmp_path / "site.toml"
        site.write_text("", encoding="utf-8")

        return ConfigFiles(config=path, site=site)

    def test_app_root_stays_untouched(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        shutil.copytree(ASSETS / ".chainlit", root / ".chainlit")
        files_dir = tmp_path / "data" / "files"
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        files = self._config(tmp_path, root, str(files_dir))
        before = self._tree(root)

        result = subprocess.run(
            [sys.executable, "-c", FILES_PROBE, str(files.config), str(files.site)],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise AssertionError(f"probe failed:\n{result.stderr}")
        if self._tree(root) != before:
            raise AssertionError(f"app_root changed: {self._tree(root)} != {before}")
        if list(cwd.iterdir()):
            raise AssertionError(f"cwd is not empty: {list(cwd.iterdir())}")
        if not files_dir.is_dir():
            raise AssertionError(f"files_dir is not created: {files_dir}")

        expected = [str(files_dir), str(files_dir), str(files_dir / "s1")]
        if result.stdout.split() != expected:
            raise AssertionError(f"{result.stdout.split()} != {expected}")

    def test_empty_files_dir_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ChainlitEnv.APP_ROOT, str(tmp_path))

        with pytest.raises(ValueError, match="files_dir"):
            AppEntry.attachments(self._config(tmp_path, tmp_path, ""))
