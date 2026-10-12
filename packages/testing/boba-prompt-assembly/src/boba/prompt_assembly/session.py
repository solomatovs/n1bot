"""Сборка слоёв пакета над одним журналом и внешние параметры запуска.

PromptSession связывает журнал, загрузчик, сборку запроса и модель сжатия в
один объект; им пользуются вход командной строки и сверка. Здесь же граница
внешних параметров: то, чего в журнале нет (каталог модели, беты входа,
режим показа мышления, TTL меток, серверные флаги, переменные порога сжатия),
читается из файла JSON либо из именованного набора и дальше живёт моделью
SessionOptions.

Ошибки:
SessionError — файл параметров не читается или не разбирается, параметров
    не хватает (текст перечисляет недостающие и объясняет, почему их нет в
    журнале) либо слой под сессией не собрался; ошибки нижних слоёв
    (JournalError, LoaderError, RequestError, CompactionError) приходят
    упакованными в неё.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from boba.prompt_assembly.compaction import (
    CheckTimeline,
    CompactionError,
    CompactionLog,
    CompactionModel,
    CompactionReplay,
    IdleClearing,
    SummaryPrompt,
    SummaryRequestBuilder,
    ThresholdPolicy,
)
from boba.prompt_assembly.loader import LoaderError, SessionMemory
from boba.prompt_assembly.records import Encoding, Journal, JournalError, Json, RecordType
from boba.prompt_assembly.request import (
    Beta,
    LaunchParams,
    LaunchProfile,
    LaunchProfiles,
    ModelFacts,
    RequestBuilder,
    RequestError,
    ThinkingParam,
)


class SessionError(Exception):
    """Сессия над журналом не собрана либо внешние параметры неполны."""


class Param(StrEnum):
    """Ключи файла внешних параметров запуска."""

    PROFILE = "profile"
    MODELS = "models"
    THINKING = "thinking"
    ENVIRONMENT_BETAS = "environment_betas"
    MAIN_CACHE_TTL = "main_cache_ttl"
    SUBAGENT_CACHE_TTL = "subagent_cache_ttl"
    COMPACT_CACHE_TTL = "compact_cache_ttl"
    DEFERRED_STUB_TOOL = "deferred_stub_tool"
    ADVISOR_DEFERRED = "advisor_deferred"
    SUBAGENT_CACHE_EVICT = "subagent_cache_evict"
    SERVER_SIDE_FALLBACK = "server_side_fallback"
    DEFAULT_EFFORT = "default_effort"
    CLI_PREFIX = "cli_prefix"
    SYSTEM_CONTEXT = "system_context"
    SAFEGUARDS = "safeguards"
    FORK_CACHE_PIN = "fork_cache_pin"
    TOOL_CLEARING_MODE = "tool_clearing_mode"
    ECHO_API_BLOCK_ORDER = "echo_api_block_order"
    COMPACTION = "compaction"

    def reason(self) -> str:
        """Почему параметра нет в журнале."""
        reasons = {
            Param.MODELS: "model catalog facts (max_tokens and model betas) are never written to the journal",
            Param.THINKING: "thinking type and display follow the model and the output mode, not the history",
            Param.ENVIRONMENT_BETAS: "betas of the login kind, provider and server flags are process state",
            Param.MAIN_CACHE_TTL: "cache TTL is decided by the subscription and a server allowlist",
            Param.SUBAGENT_CACHE_TTL: "cache TTL is decided by the subscription and a server allowlist",
            Param.COMPACT_CACHE_TTL: "cache TTL is decided by the subscription and a server allowlist",
            Param.DEFERRED_STUB_TOOL: "server flag tengu_deferred_stub_tool; the stub is not recorded anywhere",
            Param.ADVISOR_DEFERRED: "advisor state of the process; the journal keeps only the advisor model",
            Param.SUBAGENT_CACHE_EVICT: "environment variable or flag plus a latch cleared by an API refusal",
            Param.SERVER_SIDE_FALLBACK: "server flag behind fallbacks and two betas of the summary request",
            Param.DEFAULT_EFFORT: "session effort is unknown until the first assistant record",
            Param.CLI_PREFIX: "the prefix is recorded only by the snapshot written after the first response",
            Param.SYSTEM_CONTEXT: "systemContext is appended after the snapshot and is not recorded",
            Param.SAFEGUARDS: "classifier context is built from settings and environment",
            Param.FORK_CACHE_PIN: "server flag tengu_basalt_spur (second cache mark of a request sent without a thread)",
            Param.TOOL_CLEARING_MODE: "server flag tengu_zany_pike (clear_tool_uses after an idle gap)",
            Param.ECHO_API_BLOCK_ORDER: "CLAUDE_CODE_STELLAR_DRIFT / tengu_stellar_drift is process state",
            Param.COMPACTION: "context window, output limit and threshold overrides come from the model catalog and environment",
        }
        return reasons.get(self, "not recorded in the journal")


class Nested(StrEnum):
    """Ключи вложенных объектов файла параметров: факты модели и параметр thinking."""

    MAX_TOKENS = "max_tokens"
    BETAS = "betas"
    TYPE = "type"
    DISPLAY = "display"


class Limits(StrEnum):
    """Ключи раздела compaction файла параметров."""

    CONTEXT_WINDOW = "context_window"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    CHARS_PER_TOKEN = "chars_per_token"
    WINDOW_OVERRIDE = "window_override"
    PCT_OVERRIDE = "pct_override"
    BLOCKING_OVERRIDE = "blocking_override"


class Source(StrEnum):
    """Постоянные сессии: текст инструкции суммаризации и источник запроса основного цикла."""

    SUMMARY_PROMPT = "texts/summarize_instruction.txt"
    QUERY = "sdk"


@dataclass(frozen=True)
class CompactionSettings:
    """Внешние параметры модели сжатия: окно и пределы модели, переменные порога процесса.

    window_override — CLAUDE_CODE_AUTO_COMPACT_WINDOW, pct_override —
    CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, blocking_override —
    CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE.
    """

    context_window: int
    max_output_tokens: int
    chars_per_token: int
    window_override: int | None = None
    pct_override: float | None = None
    blocking_override: int | None = None

    def __post_init__(self) -> None:
        if self.context_window <= 0 or self.max_output_tokens <= 0 or self.chars_per_token <= 0:
            raise SessionError(
                "compaction settings expect positive context_window, max_output_tokens and chars_per_token, "
                f"got {self.context_window}, {self.max_output_tokens}, {self.chars_per_token}"
            )


@dataclass(frozen=True)
class SessionOptions:
    """Всё, что сессии нужно сверх журнала."""

    launch: LaunchParams
    echo_api_block_order: bool
    compaction: CompactionSettings


class OptionsReader:
    """Разбор внешних параметров запуска из именованного набора, файла JSON и явных ключей.

    Набор (profile) даёт все значения сразу; файл и явные ключи их
    переопределяют. Без набора обязательны все ключи: недостающие
    перечисляются одной ошибкой. Создаётся входом командной строки и
    манифестом корпуса.
    """

    def __init__(self) -> None:
        self._profiles = LaunchProfiles()

    def default_compaction(self) -> CompactionSettings:
        """Пределы claude-opus-5-5 без переменных порога."""
        return CompactionSettings(context_window=1_000_000, max_output_tokens=128_000, chars_per_token=3)

    def profile(self, name: str) -> SessionOptions:
        return SessionOptions(
            launch=self._launch_profile(name), echo_api_block_order=False, compaction=self.default_compaction()
        )

    def combine(
        self, base: SessionOptions, echo_api_block_order: bool, tool_clearing_mode: str, compaction: CompactionSettings
    ) -> SessionOptions:
        """Набор с заменой того, что задано отдельно от него (манифест корпуса)."""
        launch = replace(base.launch, tool_clearing_mode=tool_clearing_mode)
        return SessionOptions(launch=launch, echo_api_block_order=echo_api_block_order, compaction=compaction)

    def file(self, path: Path) -> Mapping[str, Json]:
        try:
            document: Json = json.loads(path.read_text(encoding=Encoding.UTF8))
        except (OSError, ValueError) as exc:
            raise SessionError(f"reading launch parameters {path} failed: {exc}") from exc

        if not isinstance(document, dict):
            raise SessionError(f"{path}: launch parameters expect a JSON object, got {type(document).__name__}")

        return document

    def read(self, document: Mapping[str, Json], where: str) -> SessionOptions:
        """Параметры из разобранного JSON; where — откуда он взят, для текста ошибок."""
        known = self._values(Param)
        unknown = sorted(set(document) - set(known))
        if unknown:
            raise SessionError(f"{where}: unknown launch parameter(s) {unknown}, known are {known}")

        base: SessionOptions | None = None
        name = document.get(Param.PROFILE)
        if isinstance(name, str):
            base = self.profile(name)

        if base is None:
            self._require_all(document, where)
            base = self.profile(LaunchProfile.SUBSCRIPTION_PRINT)

        launch = base.launch
        for key, value in document.items():
            launch = self._with(launch, Param(key), value, where)

        echo = base.echo_api_block_order
        if Param.ECHO_API_BLOCK_ORDER in document:
            echo = self._flag(document, Param.ECHO_API_BLOCK_ORDER, where)

        compaction = base.compaction
        if Param.COMPACTION in document:
            compaction = self._compaction(document[Param.COMPACTION], compaction, where)

        return SessionOptions(launch=launch, echo_api_block_order=echo, compaction=compaction)

    def _launch_profile(self, name: str) -> LaunchParams:
        if name == LaunchProfile.SUBSCRIPTION_PRINT:
            return self._profiles.subscription_print()

        if name == LaunchProfile.STREAM_JSON:
            return self._profiles.stream_json()

        if name == LaunchProfile.SUBAGENT_CACHE_EVICT:
            return replace(self._profiles.subscription_print(), subagent_cache_evict=True)

        if name == LaunchProfile.SERVER_CLASSIFIER:
            return self._profiles.server_classifier(())

        raise SessionError(f"launch profile {name!r} is unknown, known profiles are {self._values(LaunchProfile)}")

    def _require_all(self, document: Mapping[str, Json], where: str) -> None:
        missing: list[str] = []
        for key in Param:
            if key is Param.PROFILE or key in document:
                continue
            missing.append(f"  {key.value}: {key.reason()}")

        if not missing:
            return

        listed = "\n".join(missing)
        raise SessionError(
            f"{where}: launch parameters that the journal does not record are missing:\n{listed}\n"
            f"give them in a JSON file (--params), or start from a profile (--profile, one of {self._values(LaunchProfile)}) "
            "and override single keys (--set KEY=JSON)"
        )

    def _with(self, launch: LaunchParams, key: Param, value: Json, where: str) -> LaunchParams:
        if key is Param.MODELS:
            return replace(launch, models=self._models(value, where))

        if key is Param.THINKING:
            return replace(launch, thinking=self._thinking(value, where))

        if key is Param.ENVIRONMENT_BETAS:
            return replace(launch, environment_betas=frozenset(self._betas(value, f"{where}: {key.value}")))

        if key is Param.SAFEGUARDS:
            if not isinstance(value, list):
                raise SessionError(f"{where}: {key.value} expects a list, got {value!r}")
            return replace(launch, safeguards=tuple(value))

        texts = (
            Param.MAIN_CACHE_TTL,
            Param.SUBAGENT_CACHE_TTL,
            Param.COMPACT_CACHE_TTL,
            Param.DEFAULT_EFFORT,
            Param.CLI_PREFIX,
            Param.SYSTEM_CONTEXT,
            Param.TOOL_CLEARING_MODE,
        )
        if key in texts:
            if not isinstance(value, str):
                raise SessionError(f"{where}: {key.value} expects a string, got {value!r}")
            return replace(launch, **{key.value: value})

        flags = (
            Param.DEFERRED_STUB_TOOL,
            Param.ADVISOR_DEFERRED,
            Param.SUBAGENT_CACHE_EVICT,
            Param.SERVER_SIDE_FALLBACK,
            Param.FORK_CACHE_PIN,
        )
        if key in flags:
            if not isinstance(value, bool):
                raise SessionError(f"{where}: {key.value} expects true or false, got {value!r}")
            return replace(launch, **{key.value: value})

        return launch

    def _models(self, value: Json, where: str) -> dict[str, ModelFacts]:
        if not isinstance(value, dict):
            raise SessionError(f"{where}: models expects an object of model name to facts, got {value!r}")

        models: dict[str, ModelFacts] = {}
        for name, facts in value.items():
            if not isinstance(facts, dict):
                raise SessionError(f"{where}: models.{name} expects an object with max_tokens and betas, got {facts!r}")
            limit = facts.get(Nested.MAX_TOKENS)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise SessionError(f"{where}: models.{name}.max_tokens expects an integer, got {limit!r}")
            betas = frozenset(self._betas(facts.get(Nested.BETAS), f"{where}: models.{name}.betas"))
            models[name] = ModelFacts(max_tokens=limit, betas=betas)

        return models

    def _thinking(self, value: Json, where: str) -> ThinkingParam:
        if not isinstance(value, dict):
            raise SessionError(f"{where}: thinking expects an object with type and display, got {value!r}")

        kind = value.get(Nested.TYPE)
        display = value.get(Nested.DISPLAY, "")
        if not isinstance(kind, str) or not isinstance(display, str):
            raise SessionError(f"{where}: thinking expects type and display as strings, got {kind!r} and {display!r}")

        return ThinkingParam(kind=kind, display=display)

    def _betas(self, value: Json, where: str) -> Iterator[Beta]:
        if not isinstance(value, list):
            raise SessionError(f"{where} expects a list of beta header values, got {value!r}")

        known = frozenset(Beta)
        for item in value:
            if item not in known:
                raise SessionError(f"{where} expects known beta header values, got {item!r}; known are {self._values(Beta)}")
            yield Beta(str(item))

    def _flag(self, document: Mapping[str, Json], key: Param, where: str) -> bool:
        value = document.get(key)
        if not isinstance(value, bool):
            raise SessionError(f"{where}: {key.value} expects true or false, got {value!r}")

        return value

    def _compaction(self, value: Json, base: CompactionSettings, where: str) -> CompactionSettings:
        if not isinstance(value, dict):
            raise SessionError(f"{where}: compaction expects an object, got {value!r}")

        known = self._values(Limits)
        unknown = sorted(set(value) - set(known))
        if unknown:
            raise SessionError(f"{where}: compaction has unknown key(s) {unknown}, known are {known}")

        pct = base.pct_override
        if Limits.PCT_OVERRIDE in value:
            pct = self._number(value[Limits.PCT_OVERRIDE], f"{where}: compaction.{Limits.PCT_OVERRIDE.value}")

        return CompactionSettings(
            context_window=self._required(value, Limits.CONTEXT_WINDOW, base.context_window, where),
            max_output_tokens=self._required(value, Limits.MAX_OUTPUT_TOKENS, base.max_output_tokens, where),
            chars_per_token=self._required(value, Limits.CHARS_PER_TOKEN, base.chars_per_token, where),
            window_override=self._optional(value, Limits.WINDOW_OVERRIDE, base.window_override, where),
            pct_override=pct,
            blocking_override=self._optional(value, Limits.BLOCKING_OVERRIDE, base.blocking_override, where),
        )

    def _values(self, members: type[StrEnum]) -> list[str]:
        values: list[str] = []
        for member in members:
            values.append(member.value)

        return sorted(values)

    def _required(self, holder: Mapping[str, Json], key: Limits, fallback: int, where: str) -> int:
        found = self._optional(holder, key, fallback, where)
        if found is None:
            raise SessionError(f"{where}: compaction.{key.value} expects an integer, got null")

        return found

    def _optional(self, holder: Mapping[str, Json], key: Limits, fallback: int | None, where: str) -> int | None:
        if key not in holder:
            return fallback

        value = holder[key]
        if value is None:
            return None

        if isinstance(value, bool) or not isinstance(value, int):
            raise SessionError(f"{where}: compaction.{key.value} expects an integer or null, got {value!r}")

        return value

    def _number(self, value: Json, where: str) -> float | None:
        if value is None:
            return None

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SessionError(f"{where} expects a number or null, got {value!r}")

        return float(value)


class PromptSession:
    """Все слои пакета над одним журналом: память процесса, сборка запроса, модель сжатия.

    Создаётся фабрикой Sessions из прочитанного журнала и внешних параметров
    и раздаёт журнал загрузчику (SessionMemory), сборке тела
    (RequestBuilder), запросу суммаризации (SummaryRequestBuilder) и модели
    сжатия (CompactionModel). Им пользуются вход командной строки и сверка.
    """

    def __init__(self, journal: Journal, options: SessionOptions) -> None:
        self.options = options
        self.journal = journal

        try:
            self.memory = SessionMemory(journal)
            self.log = CompactionLog(self.memory)
            self.clearing = IdleClearing(self.log, options.launch.tool_clearing_mode)
            self.requests = RequestBuilder(self.memory, options.launch, self.clearing, options.echo_api_block_order)
            self.prompt = SummaryPrompt(Path(__file__).resolve().parent / Source.SUMMARY_PROMPT)
            self.summaries = SummaryRequestBuilder(self.requests, options.launch, self.prompt)
            self.policy = ThresholdPolicy(
                context_window=options.compaction.context_window,
                max_output_tokens=options.compaction.max_output_tokens,
                window_override=options.compaction.window_override,
                pct_override=options.compaction.pct_override,
                blocking_override=options.compaction.blocking_override,
            )
            self.model = CompactionModel(self.policy, options.compaction.chars_per_token)
        except (LoaderError, RequestError, CompactionError) as exc:
            raise SessionError(f"opening a session over {journal.path} failed: {exc}") from exc

    def request_lines(self) -> list[int]:
        """Строки запросов, найденные по истории."""
        lines: list[int] = []
        for moment in self.memory.moments():
            lines.append(moment.line)

        return lines

    def replay(self, request_lines: Sequence[int]) -> CompactionReplay:
        """Проигрывание проверок порога перед запросами с данных строк."""
        return CompactionReplay(self.model, CheckTimeline(self.log, request_lines), Source.QUERY)


class Sessions:
    """Фабрика сессий: читает журнал один раз и строит над ним сессии с разными внешними параметрами."""

    def open(self, path: Path, options: SessionOptions) -> PromptSession:
        journal = Journal(path)

        try:
            journal.load()
        except JournalError as exc:
            raise SessionError(f"opening a session over {path} failed: {exc}") from exc

        return PromptSession(journal, options)

    def without_oracle(self, session: PromptSession) -> PromptSession:
        """Сессия над тем же журналом без записей эталона (api-request, shape, blob)."""
        kinds = frozenset((RecordType.REQUEST.value, RecordType.SHAPE.value, RecordType.BLOB.value))
        return PromptSession(session.journal.without(kinds), session.options)

    def variant(self, session: PromptSession, launch: LaunchParams) -> PromptSession:
        """Сессия над тем же журналом с другими параметрами запуска (состояние процесса менялось между запросами)."""
        return PromptSession(session.journal, replace(session.options, launch=launch))
