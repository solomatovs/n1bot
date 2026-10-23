"""Опись данных пакетов и самопроверка по ней без сборки бинарника: дерево
установки собирается из исходников пакетов boba-cli, boba-agent и
boba-history-postgres так, как его раскладывает установщик колёс (каталоги
пакетов и dist-info с RECORD); вход модуля boba.cli.package_data пишет опись
и флаги Nuitka, `boba self-check` над тем же деревом находит все файлы описи
и называет пропавший. Из дерева исходников описи нет, и самопроверка это
говорит."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import boba.agent
import boba.cli
import boba.history.postgres


class InstalledTree:
    """Каталог site-packages из исходников пакетов boba: копия каталога
    обычного пакета и dist-info с METADATA и RECORD, как после установки
    колеса; run() запускает модуль интерпретатором над этим деревом."""

    TIMEOUT_SEC: ClassVar[float] = 120.0

    def __init__(self, site: Path, home: Path) -> None:
        self.site = site
        self._home = home

    def install(self, name: str, package: ModuleType) -> None:
        source = Path(str(package.__file__)).parent
        target = self.site.joinpath(*package.__name__.split("."))
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))

        info = self.site / f"{name.replace('-', '_')}-0.dist-info"
        info.mkdir()
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 0\n"
        (info / "METADATA").write_text(metadata, encoding="utf-8")

        rows: list[str] = []
        for row in self._recorded(target, info):
            rows.append(f"{row},,\n")

        (info / "RECORD").write_text("".join(rows), encoding="utf-8")

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(self._home),
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(self.site),
        }

        return subprocess.run(
            [sys.executable, "-m", *argv],
            capture_output=True,
            text=True,
            timeout=self.TIMEOUT_SEC,
            check=False,
            env=environment,
            cwd=self._home,
        )

    def _recorded(self, target: Path, info: Path) -> Iterator[str]:
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue

            yield path.relative_to(self.site).as_posix()

        yield (info / "METADATA").relative_to(self.site).as_posix()
        yield (info / "RECORD").relative_to(self.site).as_posix()


class TestPackageData:
    SCHEMA: ClassVar[str] = "schema/10_history.sql"

    def test_survey_names_the_data_and_self_check_finds_it(
        self, tmp_path: Path
    ) -> None:
        site = tmp_path / "site"
        (tmp_path / "home").mkdir()
        tree = InstalledTree(site, tmp_path / "home")
        tree.install("boba-cli", boba.cli)
        tree.install("boba-agent", boba.agent)
        tree.install("boba-history-postgres", boba.history.postgres)
        manifest = tmp_path / "package_data.json"

        surveyed = tree.run("boba.cli.package_data", str(site), str(manifest))

        assert surveyed.returncode == 0, surveyed.stderr[-2000:]
        assert surveyed.stdout.splitlines() == [
            "--include-package-data=boba.agent",
            "--include-package-data=boba.history.postgres",
            f"--include-data-files={manifest}=boba/cli/package_data.json",
        ]
        named = self._named(manifest)
        assert f"boba.history.postgres:{self.SCHEMA}" in named
        assert "boba.agent:template/attachments/date.txt" in named

        shutil.copy(manifest, site / "boba" / "cli" / "package_data.json")
        checked = tree.run("boba.cli.app", "self-check")

        assert checked.returncode == 0, checked.stderr[-2000:]
        assert checked.stdout == (
            f"self-check: {len(named)} package data files present "
            "(boba.agent, boba.history.postgres)\n"
        )

        (site / "boba" / "history" / "postgres" / self.SCHEMA).unlink()
        broken = tree.run("boba.cli.app", "self-check")

        assert broken.returncode == 1
        assert f"1 of {len(named)} package data files" in broken.stderr
        assert f"boba.history.postgres:{self.SCHEMA} (no file under" in broken.stderr

    def test_self_check_in_the_source_tree_reports_no_manifest(
        self, tmp_path: Path
    ) -> None:
        checked = subprocess.run(
            [sys.executable, "-m", "boba.cli.app", "self-check"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=tmp_path,
        )

        assert checked.returncode == 1
        assert "the package data manifest" in checked.stderr
        assert "is missing" in checked.stderr

    def test_survey_of_a_tree_without_boba_wheels_fails(self, tmp_path: Path) -> None:
        tree = InstalledTree(tmp_path, tmp_path)

        surveyed = tree.run(
            "boba.cli.package_data", str(tmp_path), str(tmp_path / "out.json")
        )

        assert surveyed.returncode == 1
        assert "carry no data files" in surveyed.stderr
        assert not (tmp_path / "out.json").exists()

    @staticmethod
    def _named(manifest: Path) -> Sequence[str]:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        named: list[str] = []
        for resource in document["resources"]:
            named.append(f"{resource['package']}:{resource['path']}")

        return named
