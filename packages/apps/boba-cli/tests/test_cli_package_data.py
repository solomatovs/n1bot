"""Опись сборки и самопроверка по ней без сборки бинарника: дерево
установки собирается из исходников пакетов boba-cli, boba-runtime, boba-agent
и boba-history-postgres так, как его раскладывает установщик колёс (каталоги
пакетов и dist-info с RECORD и entry_points.txt); вход модуля
boba.runtime.program пишет опись и флаги Nuitka, `boba self-check` над тем же
деревом находит все файлы и entry points описи и называет пропавший. Из
дерева исходников описи нет, и самопроверка это говорит."""

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
import boba.runtime


class InstalledTree:
    """Каталог site-packages из исходников пакетов boba: копия каталога
    обычного пакета и dist-info с METADATA и RECORD, как после установки
    колеса; run() запускает модуль интерпретатором над этим деревом."""

    TIMEOUT_SEC: ClassVar[float] = 120.0

    def __init__(self, site: Path, home: Path) -> None:
        self.site = site
        self._home = home

    def install(self, name: str, package: ModuleType, entry_points: str = "") -> None:
        source = Path(str(package.__file__)).parent
        target = self.site.joinpath(*package.__name__.split("."))
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))

        info = self.site / f"{name.replace('-', '_')}-0.dist-info"
        info.mkdir()
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 0\n"
        (info / "METADATA").write_text(metadata, encoding="utf-8")
        if entry_points:
            (info / "entry_points.txt").write_text(entry_points, encoding="utf-8")

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

        for path in sorted(info.iterdir()):
            yield path.relative_to(self.site).as_posix()

        yield (info / "RECORD").relative_to(self.site).as_posix()


class TestPackageData:
    SCHEMA: ClassVar[str] = "schema/10_history.sql"
    ENTRY_POINTS: ClassVar[str] = (
        "[boba.history]\nstore = boba.history.postgres:PostgresHistoryStore\n\n"
        "[console_scripts]\nboba-history = boba.history.postgres:main\n"
    )
    """Своя группа boba.* идёт в опись, console_scripts — нет."""

    def test_survey_names_the_data_and_self_check_finds_it(
        self, tmp_path: Path
    ) -> None:
        site = tmp_path / "site"
        (tmp_path / "home").mkdir()
        tree = InstalledTree(site, tmp_path / "home")
        tree.install("boba-cli", boba.cli)
        tree.install("boba-runtime", boba.runtime)
        tree.install("boba-agent", boba.agent)
        tree.install("boba-history-postgres", boba.history.postgres, self.ENTRY_POINTS)
        manifest = tmp_path / "program.json"

        surveyed = tree.run("boba.runtime.program", str(site), str(manifest))

        assert surveyed.returncode == 0, surveyed.stderr[-2000:]
        assert surveyed.stdout.splitlines() == [
            "--include-module=boba.agent",
            "--include-package-data=boba.agent",
            "--include-module=boba.history.postgres",
            "--include-package-data=boba.history.postgres",
            "--include-module=boba.runtime",
            "--include-package-data=boba.runtime",
            "--include-distribution-metadata=boba-history-postgres",
            "--include-module=boba.history.postgres",
            f"--include-data-files={manifest}=boba/runtime/program.json",
        ]
        named = self._named(manifest)
        assert f"boba.history.postgres:{self.SCHEMA}" in named
        assert "boba.agent:template/attachments/date.txt" in named
        assert "boba.runtime:schema/10_users.sql" in named

        shutil.copy(manifest, site / "boba" / "runtime" / "program.json")
        checked = tree.run("boba.cli.app", "self-check")

        assert checked.returncode == 0, checked.stderr[-2000:]
        assert checked.stdout == (
            f"self-check: {len(named)} package data files present "
            "(boba.agent, boba.history.postgres, boba.runtime); "
            "1 entry points load (boba.history)\n"
        )

        (site / "boba" / "history" / "postgres" / self.SCHEMA).unlink()
        broken = tree.run("boba.cli.app", "self-check")

        assert broken.returncode == 1
        assert f"1 of {len(named)} package data files" in broken.stderr
        assert f"boba.history.postgres:{self.SCHEMA} (no file under" in broken.stderr

    def test_self_check_names_an_entry_point_that_does_not_load(
        self, tmp_path: Path
    ) -> None:
        site = tmp_path / "site"
        (tmp_path / "home").mkdir()
        tree = InstalledTree(site, tmp_path / "home")
        tree.install("boba-cli", boba.cli)
        tree.install("boba-runtime", boba.runtime)
        tree.install(
            "boba-history-postgres",
            boba.history.postgres,
            "[boba.history]\nstore = boba.history.postgres:NoSuchStore\n",
        )
        manifest = tmp_path / "program.json"
        surveyed = tree.run("boba.runtime.program", str(site), str(manifest))
        assert surveyed.returncode == 0, surveyed.stderr[-2000:]
        shutil.copy(manifest, site / "boba" / "runtime" / "program.json")

        checked = tree.run("boba.cli.app", "self-check")

        assert checked.returncode == 1
        assert "1 of 1 entry points named by the build manifest do not load" in (
            checked.stderr
        )
        assert "boba.history:store = boba.history.postgres:NoSuchStore" in (
            checked.stderr
        )

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
        assert "the build manifest" in checked.stderr
        assert "is missing" in checked.stderr

    def test_survey_of_a_tree_without_boba_wheels_fails(self, tmp_path: Path) -> None:
        tree = InstalledTree(tmp_path, tmp_path)

        surveyed = tree.run(
            "boba.runtime.program", str(tmp_path), str(tmp_path / "out.json")
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
