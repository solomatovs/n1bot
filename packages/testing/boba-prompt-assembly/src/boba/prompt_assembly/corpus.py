"""Манифест корпуса сверки: какие журналы проверять и с какими внешними параметрами.

Манифест — файл JSON рядом с описанием корпуса. Для каждого журнала он
называет адрес (каталог проекта + id сессии), сценарий, строку, после которой
записи эталона действительны, набор внешних параметров запуска и условия
прогона для модели сжатия (переменные порога, каталоги тел с провода,
отладочные журналы). Разбирается один раз на входе в модели.

Ошибки:
VerifyError — манифест не читается или не проходит разбор; текст называет
    файл, поле и полученное значение. Общая ошибка слоя сверки: её же
    поднимают модули verify и compaction_checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from boba.prompt_assembly.records import Encoding, JournalAddress, Json
from boba.prompt_assembly.session import CompactionSettings, OptionsReader, SessionError, SessionOptions


class VerifyError(Exception):
    """Сверка не выполнена: вход не читается либо слой под сверкой сорвался."""


class Field(StrEnum):
    """Ключи манифеста корпуса."""

    PROJECTS_ROOT = "projects_root"
    MODEL = "model"
    CONTEXT_WINDOW = "context_window"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    CHARS_PER_TOKEN = "chars_per_token"
    TOOL_CLEARING_MODE = "tool_clearing_mode"
    JOURNALS = "journals"
    PROJECT = "project"
    SESSION = "session"
    SCENARIO = "scenario"
    VALID_AFTER = "oracle_valid_after_line"
    LAUNCH = "launch"
    PROFILE = "profile"
    ECHO = "echo_api_block_order"
    SAFEGUARDS = "safeguards"
    FROM_ORACLE = "from_oracle"
    REQUEST_PROFILES = "request_profiles"
    COMPACTION = "compaction"
    WINDOW_OVERRIDE = "window_override"
    PCT_OVERRIDE = "pct_override"
    BLOCKING_OVERRIDE = "blocking_override"
    WIRE_DIRS = "wire_dirs"
    WIRE_SKIP = "wire_skip"
    DEBUG_LOGS = "debug_logs"
    DEBUG_SKIP = "debug_skip"
    DEBUG_TAIL = "debug_tail"
    LOADER_CASE = "loader_case"
    HOME = "~"


@dataclass(frozen=True)
class LaunchSpec:
    """Внешние параметры запуска журнала корпуса.

    profile — имя набора LaunchProfile; request_profiles — набор для
    отдельных запросов по строке записи эталона (состояние процесса менялось
    между запросами); safeguards_from_oracle — параметр safeguards берётся
    из эталона: в истории его нет.
    """

    profile: str
    echo_api_block_order: bool
    safeguards_from_oracle: bool
    request_profiles: Mapping[int, str]


@dataclass(frozen=True)
class CompactionSpec:
    """Условия прогона журнала для сверки модели сжатия.

    wire_dirs — каталоги тел запросов с провода (wire_skip — сколько границ
    журнала без снятого тела идут первыми), debug_logs — отладочные журналы
    процессов с уровнями проверок (debug_skip — сколько первых строк
    пропустить, debug_tail — сверять только хвост).
    """

    window_override: int | None
    pct_override: float | None
    blocking_override: int | None
    wire_dirs: tuple[Path, ...]
    wire_skip: int
    debug_logs: tuple[Path, ...]
    debug_skip: int
    debug_tail: bool


@dataclass(frozen=True)
class CorpusEntry:
    """Журнал корпуса: адрес, сценарий и условия сверки.

    valid_after — записи эталона действительны со строки, следующей за этой
    (0 — все): более ранние скопированы из чужого журнала.
    """

    address: JournalAddress
    path: Path
    scenario: str
    valid_after: int
    launch: LaunchSpec
    compaction: CompactionSpec | None
    loader_case: str


@dataclass(frozen=True)
class CorpusManifest:
    """Разобранный манифест: журналы и общие факты о модели."""

    path: Path
    projects: Path
    limits: CompactionSettings
    tool_clearing_mode: str
    entries: tuple[CorpusEntry, ...]


class ManifestReader:
    """Чтение манифеста корпуса из JSON в модели; создаётся сверкой."""

    def __init__(self) -> None:
        self._options = OptionsReader()

    def read(self, path: Path) -> CorpusManifest:
        try:
            document: Json = json.loads(path.read_text(encoding=Encoding.UTF8))
        except (OSError, ValueError) as exc:
            raise VerifyError(f"reading corpus manifest {path} failed: {exc}") from exc

        if not isinstance(document, dict):
            raise VerifyError(f"{path}: corpus manifest expects a JSON object, got {type(document).__name__}")

        root = document.get(Field.PROJECTS_ROOT)
        if not isinstance(root, str) or not root:
            raise VerifyError(f"{path}: {Field.PROJECTS_ROOT.value} expects a non-empty path string, got {root!r}")

        projects = Path(root)
        if root.startswith(Field.HOME):
            projects = projects.expanduser()

        journals = document.get(Field.JOURNALS)
        if not isinstance(journals, list):
            raise VerifyError(f"{path}: {Field.JOURNALS.value} expects a list of journal objects, got {journals!r}")

        mode = document.get(Field.TOOL_CLEARING_MODE)
        if not isinstance(mode, str):
            raise VerifyError(f"{path}: {Field.TOOL_CLEARING_MODE.value} expects a string, got {mode!r}")

        entries: list[CorpusEntry] = []
        for item in journals:
            if not isinstance(item, dict):
                raise VerifyError(f"{path}: every journal expects an object, got {item!r}")
            entries.append(self._entry(item, path, projects))

        return CorpusManifest(
            path=path,
            projects=projects,
            limits=self._limits(document.get(Field.MODEL), path),
            tool_clearing_mode=mode,
            entries=tuple(entries),
        )

    def options(self, manifest: CorpusManifest, entry: CorpusEntry, profile: str) -> SessionOptions:
        """Внешние параметры сессии над журналом корпуса для набора profile."""
        try:
            base = self._options.profile(profile)
        except SessionError as exc:
            raise VerifyError(f"{manifest.path}: journal {entry.address.label}: {exc}") from exc

        limits = manifest.limits
        spec = entry.compaction
        if spec is not None:
            limits = CompactionSettings(
                context_window=limits.context_window,
                max_output_tokens=limits.max_output_tokens,
                chars_per_token=limits.chars_per_token,
                window_override=spec.window_override,
                pct_override=spec.pct_override,
                blocking_override=spec.blocking_override,
            )

        return self._options.combine(base, entry.launch.echo_api_block_order, manifest.tool_clearing_mode, limits)

    def _entry(self, item: Mapping[str, Json], path: Path, projects: Path) -> CorpusEntry:
        project = item.get(Field.PROJECT)
        session = item.get(Field.SESSION)
        if not isinstance(project, str) or not project or not isinstance(session, str) or not session:
            raise VerifyError(
                f"{path}: a journal expects {Field.PROJECT.value} and {Field.SESSION.value} as non-empty strings, "
                f"got {project!r} and {session!r}"
            )

        address = JournalAddress(project=project, session=session)
        scenario = item.get(Field.SCENARIO)
        if not isinstance(scenario, str):
            raise VerifyError(f"{path}: journal {address.label} expects {Field.SCENARIO.value} as a string, got {scenario!r}")

        case = item.get(Field.LOADER_CASE, "")
        if not isinstance(case, str):
            raise VerifyError(f"{path}: journal {address.label} expects {Field.LOADER_CASE.value} as a string, got {case!r}")

        compaction: CompactionSpec | None = None
        if Field.COMPACTION in item:
            compaction = self._compaction(item[Field.COMPACTION], path, address)

        return CorpusEntry(
            address=address,
            path=address.path(projects),
            scenario=scenario,
            valid_after=self._count(item, Field.VALID_AFTER, path, address),
            launch=self._launch(item.get(Field.LAUNCH), path, address),
            compaction=compaction,
            loader_case=case,
        )

    def _launch(self, value: Json, path: Path, address: JournalAddress) -> LaunchSpec:
        if not isinstance(value, dict):
            raise VerifyError(f"{path}: journal {address.label} expects {Field.LAUNCH.value} as an object, got {value!r}")

        profile = value.get(Field.PROFILE)
        if not isinstance(profile, str) or not profile:
            raise VerifyError(f"{path}: journal {address.label} expects launch.profile as a non-empty string, got {profile!r}")

        echo = value.get(Field.ECHO, False)
        if not isinstance(echo, bool):
            raise VerifyError(f"{path}: journal {address.label} expects launch.{Field.ECHO.value} as true or false, got {echo!r}")

        return LaunchSpec(
            profile=profile,
            echo_api_block_order=echo,
            safeguards_from_oracle=value.get(Field.SAFEGUARDS) == Field.FROM_ORACLE,
            request_profiles=dict(self._request_profiles(value.get(Field.REQUEST_PROFILES), path, address)),
        )

    def _request_profiles(self, value: Json, path: Path, address: JournalAddress) -> Iterator[tuple[int, str]]:
        if value is None:
            return

        if not isinstance(value, dict):
            raise VerifyError(
                f"{path}: journal {address.label} expects launch.{Field.REQUEST_PROFILES.value} as an object "
                f"of request line to profile name, got {value!r}"
            )

        for line, profile in value.items():
            if not line.isdigit() or not isinstance(profile, str):
                raise VerifyError(
                    f"{path}: journal {address.label} expects launch.{Field.REQUEST_PROFILES.value} entries as "
                    f"line number to profile name, got {line!r}: {profile!r}"
                )
            yield int(line), profile

    def _compaction(self, value: Json, path: Path, address: JournalAddress) -> CompactionSpec:
        if not isinstance(value, dict):
            raise VerifyError(f"{path}: journal {address.label} expects {Field.COMPACTION.value} as an object, got {value!r}")

        pct = value.get(Field.PCT_OVERRIDE)
        pct_override: float | None = None
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            pct_override = float(pct)

        tail = value.get(Field.DEBUG_TAIL, False)
        if not isinstance(tail, bool):
            raise VerifyError(f"{path}: journal {address.label} expects compaction.{Field.DEBUG_TAIL.value} as true or false, got {tail!r}")

        return CompactionSpec(
            window_override=self._optional(value, Field.WINDOW_OVERRIDE, path, address),
            pct_override=pct_override,
            blocking_override=self._optional(value, Field.BLOCKING_OVERRIDE, path, address),
            wire_dirs=tuple(self._paths(value, Field.WIRE_DIRS, path, address)),
            wire_skip=self._count(value, Field.WIRE_SKIP, path, address),
            debug_logs=tuple(self._paths(value, Field.DEBUG_LOGS, path, address)),
            debug_skip=self._count(value, Field.DEBUG_SKIP, path, address),
            debug_tail=tail,
        )

    def _limits(self, value: Json, path: Path) -> CompactionSettings:
        if not isinstance(value, dict):
            raise VerifyError(f"{path}: {Field.MODEL.value} expects an object with the model limits, got {value!r}")

        numbers: list[int] = []
        for key in (Field.CONTEXT_WINDOW, Field.MAX_OUTPUT_TOKENS, Field.CHARS_PER_TOKEN):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, int):
                raise VerifyError(f"{path}: model.{key.value} expects an integer, got {number!r}")
            numbers.append(number)

        try:
            return CompactionSettings(context_window=numbers[0], max_output_tokens=numbers[1], chars_per_token=numbers[2])
        except SessionError as exc:
            raise VerifyError(f"{path}: {Field.MODEL.value}: {exc}") from exc

    def _paths(self, holder: Mapping[str, Json], key: Field, path: Path, address: JournalAddress) -> Iterator[Path]:
        value = holder.get(key, [])
        if not isinstance(value, list):
            raise VerifyError(f"{path}: journal {address.label} expects compaction.{key.value} as a list of paths, got {value!r}")

        for item in value:
            if not isinstance(item, str):
                raise VerifyError(f"{path}: journal {address.label} expects compaction.{key.value} items as strings, got {item!r}")
            yield (path.parent / item).resolve()

    def _count(self, holder: Mapping[str, Json], key: Field, path: Path, address: JournalAddress) -> int:
        found = self._optional(holder, key, path, address)
        if found is None:
            return 0

        return found

    def _optional(self, holder: Mapping[str, Json], key: Field, path: Path, address: JournalAddress) -> int | None:
        value = holder.get(key)
        if value is None:
            return None

        if isinstance(value, bool) or not isinstance(value, int):
            raise VerifyError(f"{path}: journal {address.label} expects {key.value} as an integer, got {value!r}")

        return value

    def journals(self, manifest: CorpusManifest, wanted: Sequence[JournalAddress]) -> list[CorpusEntry]:
        """Журналы манифеста; wanted сужает список до перечисленных адресов."""
        if not wanted:
            return list(manifest.entries)

        chosen: list[CorpusEntry] = []
        for entry in manifest.entries:
            if entry.address in wanted:
                chosen.append(entry)

        return chosen
