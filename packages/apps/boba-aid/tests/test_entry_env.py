"""Env chainlit выставляется точкой входа до первого импорта его модулей."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from aid_stand import FakeSecret

from boba.aid.infra.entry import AppEntry, ChainlitEnv
from boba.config import ConfigError, ConfigLoader
from boba.runtime.config import ProgramRoots


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
    aid     = "${aid}"
    session = "${session}"

[instance]
    data_dir = "<data_dir>"
    ca_file  = "/etc/ssl/certs/ca-certificates.crt"

[session]
    auth_secret     = "<auth_secret>"
    cookie          = "boba_token"
    cookie_samesite = "strict"
    session_ttl_sec = 3600
    session_max_sec = 86400

[aid]
    url_prefix           = "/boba"
    shutdown_timeout_sec = 10
"""

ASSETS = Path(__file__).resolve().parents[1] / "assets"
"""Ассеты пакета: share/ программы из исходников и источник share/ образа."""

FILES_PROBE = """
import sys
from pathlib import Path
from types import SimpleNamespace

from boba.aid.infra.entry import AppEntry
from boba.config import ConfigLoader

raw = ConfigLoader([Path(sys.argv[1])]).load()
AppEntry().export_env(raw)
AppEntry().attachments(raw).install()

import chainlit.config
import chainlit.server
from chainlit.session import BaseSession

print(chainlit.config.FILES_DIRECTORY)
print(chainlit.server.FILES_DIRECTORY)
print(BaseSession.files_dir.fget(SimpleNamespace(id="s1")))
"""
"""Что видит chainlit после точки входа: каталог вложений модуля config, его
копия в chainlit.server и каталог сессии."""


def _config_file(tmp_path: Path, data_dir: Path) -> Path:
    path = tmp_path / "config.toml"
    body = CONFIG.replace("<data_dir>", str(data_dir))
    body = body.replace("<auth_secret>", FakeSecret.AUTH)
    path.write_text(body, encoding="utf-8")

    return path


class TestExportEnv:
    def test_sources_serve_the_package_assets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Из исходников корень chainlit — ассеты пакета в дереве, а не
        собранный runtime/: правка ассета видна без сборки; остальное — из
        конфига."""
        monkeypatch.delenv(ChainlitEnv.APP_ROOT, raising=False)
        monkeypatch.delenv(ChainlitEnv.ROOT_PATH, raising=False)
        monkeypatch.delenv(ChainlitEnv.AUTH_SECRET, raising=False)
        tree = tmp_path / "tree"
        (tree / "runtime").mkdir(parents=True)
        (tree / "packages").mkdir()
        monkeypatch.setenv(ProgramRoots.RUNTIME_ENV, str(tree / "runtime"))

        raw = ConfigLoader([_config_file(tmp_path, tmp_path / "data")]).load()
        AppEntry().export_env(raw)

        expected = tree.resolve() / "packages/apps/boba-aid/assets"
        assert os.environ[ChainlitEnv.APP_ROOT] == str(expected)
        assert os.environ[ChainlitEnv.ROOT_PATH] == "/boba"
        assert os.environ[ChainlitEnv.AUTH_SECRET] == FakeSecret.AUTH
        assert os.environ[ChainlitEnv.COOKIE_NAME] == "boba_token"
        assert os.environ[ChainlitEnv.COOKIE_SAMESITE] == "strict"

    def test_program_without_runtime_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ProgramRoots.RUNTIME_ENV, raising=False)
        raw = ConfigLoader([_config_file(tmp_path, tmp_path / "data")]).load()

        with pytest.raises(ConfigError, match=ProgramRoots.RUNTIME_ENV):
            AppEntry().export_env(raw)


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

    @pytest.mark.parametrize("module", ["boba.aid.infra.entry"])
    def test_module_import_leaves_cwd_clean(self, module: str, tmp_path: Path) -> None:
        result = self._probe(module, tmp_path)

        if result.stdout.strip() != "[]":
            raise AssertionError('result.stdout.strip() == "[]"')
        if (tmp_path / ".chainlit").exists():
            raise AssertionError('not (tmp_path / ".chainlit").exists()')
        if (tmp_path / ".files").exists():
            raise AssertionError('not (tmp_path / ".files").exists()')


class TestChainlitFilesOutsideAppRoot:
    """chainlit пишет вложения в files/ каталога данных экземпляра, а share/
    программы не трогает.

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

    def test_app_root_stays_untouched(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        runtime = tree / "runtime"
        runtime.mkdir(parents=True)
        root = tree / "packages/apps/boba-aid/assets"
        shutil.copytree(ASSETS / ".chainlit", root / ".chainlit")
        data_dir = tmp_path / "data"
        files_dir = data_dir / "files"
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        config = _config_file(tmp_path, data_dir)
        before = self._tree(root)
        env = dict(os.environ)
        env[ProgramRoots.RUNTIME_ENV] = str(runtime)

        result = subprocess.run(
            [sys.executable, "-c", FILES_PROBE, str(config)],
            cwd=cwd,
            env=env,
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
