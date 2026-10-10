"""Сверка модели сжатия с журналами, телами запросов с провода и отладочными журналами процессов.

Для журнала корпуса проверяет: исход каждой проверки порога, поля
compactMetadata и состав хвоста каждой границы, порядок записей после
сжатия, запрос суммаризации против тела с провода (по частям и целиком по
контрольной сумме), первый запрос после сжатия, правку clear_tool_uses и
заглушки вынесенных результатов.

Ошибки:
VerifyError — тело с провода или отладочный журнал не читаются либо слой
    под сверкой сорвался; текст называет файл, строку и причину. Ошибки
    модели сжатия, сборки запроса и декодера эталона приходят упакованными
    в неё.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from boba.prompt_assembly.compaction import (
    Attempt,
    CompactionError,
    CompactionLog,
    CompactionModel,
    CompactMark,
    FileRestorePlanner,
    IdleEdit,
    LadderStop,
    Message,
    ReadState,
    ResultPersistence,
    SummarizeLadder,
    SummaryPrompt,
    SummaryRequest,
    SummaryWrapper,
    Trigger,
)
from boba.prompt_assembly.corpus import CompactionSpec, VerifyError
from boba.prompt_assembly.oracle import JsonDiff, ReplayError, ReplayHasher, RequestRebuilder, SessionLog
from boba.prompt_assembly.records import BlockType, Encoding, Json, JsNumber, Key, RecordType
from boba.prompt_assembly.request import RequestError
from boba.prompt_assembly.session import PromptSession, SessionError, Sessions


class Name(StrEnum):
    """Постоянные сверки сжатия: заголовки запроса суммаризации, шаблоны отладочного журнала и заглушек."""

    DEBUG_LEVEL = r"autocompact: tokens=\S+ level=(\w+)"
    COMPACTION_CLASS = "compaction"
    REQUEST_CLASS = "x-claude-code-request-class"
    COMPACTION_HEADER = "x-claude-code-compaction"
    BETA_HEADER = "anthropic-beta"
    BILLING = "x-anthropic-billing-header"
    TOOL_RESULTS = "tool-results"
    SAVED_TO = r"Full output saved to: (\S+)"
    COMMAND_PREFIXES = "<local-command|<command-name>"
    WIRE_GLOB = "*.json"


class NotInDigest(StrEnum):
    """Ключи тела с провода, которых нет в контрольной сумме и в собранном теле."""

    METADATA = "metadata"
    STREAM = "stream"
    THREAD = "thread"
    DIAGNOSTICS = "diagnostics"


@dataclass(frozen=True)
class Finding:
    """Итог одной проверки: что сверялось, сошлось ли, предсказанное и записанное."""

    subject: str
    ok: bool
    detail: str


@dataclass
class Report:
    """Накопитель итогов по одному журналу; печатается VerifyCompactionCli."""

    log: Path
    findings: list[Finding] = field(default_factory=lambda: list[Finding]())

    def add(self, subject: str, ok: bool, detail: str) -> None:
        self.findings.append(Finding(subject=subject, ok=ok, detail=detail))

    def same(self, subject: str, predicted: object, recorded: object) -> None:
        self.add(subject, predicted == recorded, f"model {predicted!r}, journal {recorded!r}")

    @property
    def failed(self) -> int:
        count = 0
        for finding in self.findings:
            if not finding.ok:
                count += 1

        return count


class WireLog:
    """Тела запросов суммаризации, снятые с провода, в порядке отправки."""

    def __init__(self, dirs: Sequence[Path]) -> None:
        self._dirs = dirs

    def compactions(self) -> list[Mapping[str, Json]]:
        result: list[Mapping[str, Json]] = []

        for directory in self._dirs:
            for path in sorted(directory.glob(Name.WIRE_GLOB.value)):
                document = self._read(path)
                headers = document.get("headers")
                if not isinstance(headers, dict):
                    continue
                if headers.get(Name.REQUEST_CLASS) != Name.COMPACTION_CLASS:
                    continue
                result.append(document)

        return result

    def _read(self, path: Path) -> Mapping[str, Json]:
        try:
            raw: Json = json.loads(path.read_text(encoding=Encoding.UTF8), parse_float=JsNumber)
        except (OSError, ValueError) as exc:
            raise VerifyError(f"reading wire request {path} failed: {exc}") from exc

        if not isinstance(raw, dict):
            raise VerifyError(f"{path}: expected a JSON object with url, headers and body, got {type(raw).__name__}")

        return raw


class DebugLevels:
    """Уровни заполненности из отладочных журналов процессов (--debug-file), по порядку проверок."""

    def __init__(self, paths: Sequence[Path], skip: int) -> None:
        self._paths = paths
        self._skip = skip
        self._pattern = re.compile(Name.DEBUG_LEVEL)

    def levels(self) -> list[str]:
        result: list[str] = []

        for path in self._paths:
            try:
                text = path.read_text(encoding=Encoding.UTF8, errors="replace")
            except OSError as exc:
                raise VerifyError(f"reading debug log {path} failed: {exc}") from exc
            result.extend(self._pattern.findall(text))

        return result[self._skip :]


class SummaryWireCheck:
    """Сверка запроса суммаризации с телом, снятым с провода.

    Запрос строит SummaryRequestBuilder сессии над отрезком to_summarize;
    вывод хука PreCompact и аргумент /compact моделью не предсказываются и
    берутся с провода. Сверяет части тела и заголовки, а отдельно — тело
    целиком по контрольной сумме клиента.
    """

    def __init__(self, session: PromptSession, oracle: SessionLog, hasher: ReplayHasher, borrow_safeguards: bool) -> None:
        self._session = session
        self._oracle = oracle
        self._hasher = hasher
        self._borrow_safeguards = borrow_safeguards
        self._prompt = session.prompt
        self._sessions = Sessions()
        self._diff = JsonDiff()

    def compare(self, attempt: Attempt, wire: Mapping[str, Json], boundary: Message, process_start: int, report: Report) -> None:
        body, headers = self._parts(wire, boundary)
        wire_messages = self._messages_of(body)
        request = self._request(self._session, attempt, wire_messages, boundary, False)
        predicted = self._messages_of(request.body)
        where = f"boundary {boundary.line}: summary request"

        report.add(
            f"{where} messages",
            self._same_messages(wire_messages, predicted),
            f"{len(predicted)} predicted, {len(wire_messages)} on the wire {self._diff.first(wire_messages, predicted, 'messages')[:300]}",
        )
        report.same(f"{where} cache mark index", request.mark_index, self._marked_index(wire_messages))
        report.same(f"{where} mark has no ttl", {"type": "ephemeral"}, self._mark_of(wire_messages))
        report.same(f"{where} fallbacks", request.body.get("fallbacks"), body.get("fallbacks"))
        report.same(f"{where} thread", None, body.get("thread"))
        report.same(f"{where} system cache_control", self._controls(request.body.get("system")), self._controls(self._without_billing(body.get("system"))))

        for key in ("model", "max_tokens", "thinking", "context_management", "output_config"):
            report.same(f"{where} {key}", self._plain(request.body.get(key)), self._plain(body.get(key)))

        if self._same_process_request(boundary.line, process_start):
            report.same(f"{where} tools", self._tool_names(request.body.get("tools")), self._tool_names(body.get("tools")))

        for key, value in request.headers.items():
            report.same(f"{where} header {key}", value, headers.get(key))

    def whole(self, attempt: Attempt, wire: Mapping[str, Json], boundary: Message) -> Finding:
        """Тело целиком: контрольная сумма собранного запроса против тела с провода.

        Защёлки message-threads процесса в журнале нет — перебираются оба
        значения; параметр safeguards (его нет в истории) берётся с провода,
        когда манифест это разрешает.
        """
        body, headers = self._parts(wire, boundary)
        expected = self._expected(headers, body)
        digest = self._hasher.request(expected)
        subject = f"boundary {boundary.line}: summary request body digest"
        session = self._session
        borrowed = ""
        safeguards = body.get(Key.SAFEGUARDS)
        if self._borrow_safeguards and isinstance(safeguards, list):
            session = self._with_safeguards(safeguards, boundary)
            borrowed = ", safeguards taken from the wire"

        detail = ""
        for threads in (True, False):
            request = self._request(session, attempt, self._messages_of(body), boundary, threads)
            if self._hasher.request(request.body) == digest:
                return Finding(subject=subject, ok=True, detail=f"matched with message-threads latch {threads}{borrowed}")
            detail = self._diff.first(expected, request.body, "")

        return Finding(subject=subject, ok=False, detail=detail)

    def _with_safeguards(self, safeguards: Sequence[Json], boundary: Message) -> PromptSession:
        launch = replace(self._session.options.launch, safeguards=tuple(safeguards))

        try:
            return self._sessions.variant(self._session, launch)
        except SessionError as exc:
            raise VerifyError(f"{self._session.journal.path}: opening a session with safeguards of the boundary at line {boundary.line} failed: {exc}") from exc

    def _request(
        self, session: PromptSession, attempt: Attempt, wire_messages: Sequence[Mapping[str, Json]], boundary: Message, threads: bool
    ) -> SummaryRequest:
        custom = self._custom_instructions(wire_messages)
        trigger = Trigger(str(boundary.metadata.get("trigger")))

        try:
            return session.summaries.build(attempt.to_keep[0].line, boundary.line, custom, trigger, threads)
        except CompactionError as exc:
            raise VerifyError(f"{self._session.journal.path}: building the summary request of the boundary at line {boundary.line} failed: {exc}") from exc

    def _parts(self, wire: Mapping[str, Json], boundary: Message) -> tuple[Mapping[str, Json], Mapping[str, Json]]:
        body = wire.get("body")
        headers = wire.get("headers")
        if not isinstance(body, dict) or not isinstance(headers, dict):
            raise VerifyError(f"{self._session.journal.path}: wire request for boundary at line {boundary.line} expects body and headers as objects, got {type(body).__name__} and {type(headers).__name__}")

        return body, headers

    def _expected(self, headers: Mapping[str, Json], body: Mapping[str, Json]) -> dict[str, Json]:
        """Тело с провода в виде эталона: без billing-блока и служебных ключей, betas из заголовка."""
        expected: dict[str, Json] = {}

        for key, value in body.items():
            if key in tuple(NotInDigest):
                continue
            expected[key] = value

        expected[Key.SYSTEM] = self._without_billing(body.get(Key.SYSTEM))
        betas: list[Json] = []
        betas.extend(str(headers.get(Name.BETA_HEADER)).split(","))
        expected[Key.BETAS] = betas
        return expected

    def _same_messages(self, expected: Sequence[Mapping[str, Json]], actual: Sequence[Mapping[str, Json]]) -> bool:
        if len(expected) != len(actual):
            return False

        for left, right in zip(expected, actual):
            if self._hasher.message(left) != self._hasher.message(right):
                return False

        return True

    def _same_process_request(self, boundary_line: int, process_start: int) -> bool:
        for request in self._oracle.requests:
            if process_start <= request.line < boundary_line:
                return True

        return False

    def _custom_instructions(self, messages: Sequence[Mapping[str, Json]]) -> str:
        """Вывод хука PreCompact и аргумент /compact моделью не предсказываются — берутся с провода."""
        marker = SummaryPrompt.ADDITIONAL
        base = self._prompt.render("")

        for message in reversed(messages):
            for text in self._texts(message):
                start = text.find(marker)
                if start < 0 or not text.startswith(base[:200]):
                    continue
                end = text.rfind(SummaryPrompt.REMINDER)
                return text[start + len(marker) : end]

        return ""

    def _texts(self, message: Mapping[str, Json]) -> Iterator[str]:
        content = message.get("content")
        if isinstance(content, str):
            yield content
            return

        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                yield text

    def _messages_of(self, body: Mapping[str, Json]) -> list[dict[str, Json]]:
        messages = body.get("messages")
        result: list[dict[str, Json]] = []
        if not isinstance(messages, list):
            return result

        for message in messages:
            if isinstance(message, dict):
                result.append(message)

        return result

    def _marked_index(self, messages: Sequence[Mapping[str, Json]]) -> int:
        for position, message in enumerate(messages):
            if self._mark_in(message) is not None:
                return position

        return -1

    def _mark_of(self, messages: Sequence[Mapping[str, Json]]) -> Json:
        for message in messages:
            mark = self._mark_in(message)
            if mark is not None:
                return mark

        return None

    def _mark_in(self, message: Mapping[str, Json]) -> Json:
        content = message.get("content")
        if not isinstance(content, list):
            return None

        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                return block["cache_control"]

        return None

    def _without_billing(self, system: Json) -> Json:
        if not isinstance(system, list):
            return system

        result: list[Json] = []
        for block in system:
            if isinstance(block, dict) and str(block.get("text", "")).startswith(Name.BILLING):
                continue
            result.append(block)

        return result

    def _controls(self, system: Json) -> list[Json]:
        result: list[Json] = []
        if not isinstance(system, list):
            return result

        for block in system:
            if isinstance(block, dict):
                result.append(block.get("cache_control"))

        return result

    def _tool_names(self, tools: Json) -> list[str]:
        result: list[str] = []
        if not isinstance(tools, list):
            return result

        for tool in tools:
            if isinstance(tool, dict):
                result.append(str(tool.get("name")))

        return result

    def _plain(self, value: Json) -> str:
        return json.dumps(value, sort_keys=True, default=float)


class ReadStateBook:
    """Восстановление readFileState на момент сжатия по журналу.

    Чтение в текущем процессе даёт метку времени mtime файла, чтение из
    прошлого процесса (до возобновления) — порядок результата в журнале, а
    вложения file прошлого процесса в состояние не попадают.
    """

    def __init__(self, journal: CompactionLog) -> None:
        self._journal = journal
        self._planner = FileRestorePlanner()

    def states(self, window: Sequence[Message], boundary_line: int) -> list[ReadState]:
        process_start = self._journal.process_start(boundary_line)
        result: dict[str, ReadState] = {}

        for message in window:
            for path, stamp in self._reads(message, window, process_start):
                result[path] = ReadState(path=path, timestamp=stamp)

        return list(result.values())

    def _reads(self, message: Message, window: Sequence[Message], process_start: int) -> Iterator[tuple[str, float]]:
        if message.kind == RecordType.ATTACHMENT and message.attachment_kind == "file":
            path = message.attachment.get("filename")
            if isinstance(path, str) and message.line >= process_start:
                yield path, self._mtime(path)
            return

        for path in self._planner.read_paths([message]):
            if message.line >= process_start:
                yield path, self._mtime(path)
                continue
            yield path, self._result_time(message, window)

    def _result_time(self, call: Message, window: Sequence[Message]) -> float:
        for message in window:
            if message.parent == call.uuid and message.has_tool_result:
                return self._order_key(message)

        return self._order_key(call)

    def _order_key(self, message: Message) -> float:
        return float(message.line)

    def _mtime(self, path: str) -> float:
        try:
            return 1e9 + os.stat(path).st_mtime
        except OSError:
            return 0.0


class BoundaryVerifier:
    """Сверка одной границы сжатия: хвост, поля compactMetadata, сводка, вложения."""

    def __init__(self, journal: CompactionLog, log: Path, model: CompactionModel) -> None:
        self._journal = journal
        self._log = log
        self._model = model
        self._wrapper = SummaryWrapper()
        self._planner = FileRestorePlanner()
        self._read_states = ReadStateBook(journal)
        self._commands = tuple(Name.COMMAND_PREFIXES.split("|"))

    def attempt(self, boundary: Message, blocked: bool) -> Attempt | LadderStop:
        window = self._journal.window_before(boundary.line)

        if boundary.metadata.get("trigger") == Trigger.MANUAL:
            return self._model.manual_attempt(window)

        return SummarizeLadder(window, blocked).first()

    def verify(self, boundary: Message, attempt: Attempt, report: Report) -> None:
        window = self._journal.window_before(boundary.line)
        metadata = boundary.metadata
        where = f"boundary {boundary.line}"
        trigger = Trigger(str(metadata.get("trigger")))
        process_start = self._journal.process_start(boundary.line)
        current: list[Message] = []
        for message in window:
            if message.line >= process_start:
                current.append(message)

        surfaced = self._model.forecaster.surfaced_names(current)
        forecast = self._model.forecaster.forecast(window, attempt, trigger, surfaced)

        report.same(f"{where} preTokens", forecast.pre_tokens, metadata.get("preTokens"))
        report.same(f"{where} logicalParentUuid", forecast.logical_parent, boundary.raw.get("logicalParentUuid"))
        report.same(f"{where} parentUuid", None, boundary.raw.get("parentUuid"))

        preserved = metadata.get("preservedMessages")
        recorded_all: Json = None
        recorded_anchor: Json = None
        if isinstance(preserved, dict):
            recorded_all = preserved.get("allUuids")
            recorded_anchor = preserved.get("anchorUuid")
        report.same(f"{where} preservedMessages.allUuids", list(forecast.kept_uuids), recorded_all)

        records = self._model.layout.split(self._after(boundary), attempt.to_keep)
        report.same(f"{where} anchorUuid is the summary", records.summary.uuid, recorded_anchor)
        report.same(f"{where} context before summary", self._expected_context(trigger, records.context), self._kinds(records.context))

        if "postTokens" in metadata:
            post = self._model.forecaster.post_tokens(records.summary, self._zeroed(attempt.to_keep), records.attachments)
            report.same(f"{where} postTokens", post, metadata.get("postTokens"))
            report.same(f"{where} cumulativeDroppedTokens", forecast.cumulative_dropped(post), metadata.get("cumulativeDroppedTokens"))

        report.same(f"{where} preCompactDiscoveredTools", list(forecast.discovered_tools), metadata.get("preCompactDiscoveredTools", []))
        self._verify_summary(records.summary, attempt, where, report)
        report.add(f"{where} attachment order", self._model.layout.ordered(records.attachments), " ".join(self._kinds(records.attachments)))
        self._verify_files(window, attempt, records.attachments, boundary, where, report)
        self._verify_memory(boundary, records.in_memory(), where, report)

    def _verify_summary(self, summary: Message, attempt: Attempt, where: str, report: Report) -> None:
        content = summary.content
        if not isinstance(content, str):
            report.add(f"{where} summary wrapper", False, "summary content is not a string")
            return

        text = self._wrapper.unwrap(content, str(self._log))
        rebuilt = self._wrapper.wrap(text, str(self._log), attempt.head_truncated, True)
        report.add(f"{where} summary wrapper", bool(text) and rebuilt == content, f"wrapper rebuilt from {len(text)} chars of summary")
        report.same(f"{where} summary flags", (True, True), (summary.is_summary, summary.raw.get("isVisibleInTranscriptOnly") is True))

    def _verify_files(self, window: Sequence[Message], attempt: Attempt, attachments: Sequence[Message], boundary: Message, where: str, report: Report) -> None:
        excluded = list(self._planner.read_paths(attempt.to_keep))
        excluded.extend(self._plan_paths(attachments))
        states = self._read_states.states(window, boundary.line)
        predicted: list[str] = []
        for state in self._planner.select(states, excluded):
            predicted.append(Path(state.path).name)

        recorded: list[str] = []
        for message in attachments:
            if message.attachment_kind not in ("file", "compact_file_reference"):
                continue
            recorded.append(Path(str(message.attachment.get("filename"))).name)

        report.same(f"{where} restored files", predicted, recorded)

    def _verify_memory(self, boundary: Message, predicted: Sequence[Message], where: str, report: Report) -> None:
        """Окно после сжатия против перешивки хвоста при чтении журнала."""
        end = boundary.line + 1
        if predicted:
            for message in predicted:
                end = max(end, message.line + 1)

        actual = self._journal.window_before(end)
        expected_uuids: list[str] = []
        for message in predicted:
            expected_uuids.append(message.uuid)

        known = frozenset(expected_uuids)
        actual_uuids: list[str] = []
        for message in actual:
            if message.kind == RecordType.USER and message.uuid not in known:
                continue
            actual_uuids.append(message.uuid)

        report.same(f"{where} window order after compaction", expected_uuids, actual_uuids)

    def _plan_paths(self, attachments: Sequence[Message]) -> Iterator[str]:
        for message in attachments:
            path = message.attachment.get("planFilePath")
            if isinstance(path, str):
                yield path

    def _after(self, boundary: Message) -> list[Message]:
        result: list[Message] = []
        summary_seen = False

        for message in self._journal.messages:
            if message.line < boundary.line:
                continue
            if message.line > boundary.line and message.is_boundary:
                break
            if summary_seen and message.kind == RecordType.ASSISTANT:
                break
            if summary_seen and message.kind == RecordType.USER and not self._is_command(message):
                break
            if message.is_summary:
                summary_seen = True
            result.append(message)

        return result

    def _is_command(self, message: Message) -> bool:
        content = message.content
        if not isinstance(content, str):
            return False

        return content.startswith(self._commands)

    def _expected_context(self, trigger: Trigger, context: Sequence[Message]) -> list[str]:
        if trigger is Trigger.MANUAL:
            return []

        return ["session_context", "date"]

    def _kinds(self, messages: Sequence[Message]) -> list[str]:
        kinds: list[str] = []
        for message in messages:
            kinds.append(message.attachment_kind)

        return kinds

    def _zeroed(self, messages: Sequence[Message]) -> list[Message]:
        zeroed: list[Message] = []
        for message in messages:
            zeroed.append(message.zeroed())

        return zeroed


class StubVerifier:
    """Сверка заглушек <persisted-output> с файлами tool-results сессии."""

    def __init__(self, journal: CompactionLog, log: Path) -> None:
        self._journal = journal
        self._directory = log.with_suffix("") / Name.TOOL_RESULTS
        self._persistence = ResultPersistence()
        self._saved_to = re.compile(Name.SAVED_TO)

    def verify(self, report: Report) -> None:
        for message in self._journal.messages:
            for block in message.blocks:
                self._verify_block(message, block, report)

    def _verify_block(self, message: Message, block: Mapping[str, Json], report: Report) -> None:
        if block.get("type") != BlockType.TOOL_RESULT:
            return

        content = block.get("content")
        text = self._text(content)
        if not text.startswith(CompactMark.PERSISTED_OPEN):
            return

        match = self._saved_to.search(text)
        if match is None:
            report.add(f"line {message.line}: persisted stub", False, "no saved path in the stub")
            return

        path = Path(match.group(1))
        try:
            stored = path.read_text(encoding=Encoding.UTF8)
        except OSError as exc:
            report.add(f"line {message.line}: persisted stub", False, f"reading {path} failed: {exc}")
            return

        rebuilt = self._persistence.stub(str(path), stored)
        report.add(f"line {message.line}: persisted stub text ({path.name}, {len(stored)} chars)", rebuilt == text, f"{len(rebuilt)} chars rebuilt, {len(text)} in the journal")
        report.same(f"line {message.line}: stub file lives in tool-results", self._directory, path.parent)

    def _text(self, content: Json) -> str:
        if isinstance(content, str):
            return content

        if not isinstance(content, list):
            return ""

        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return str(item.get("text"))

        return ""


class CompactionVerifier:
    """Сверка одного журнала: проверки порога, границы, запрос суммаризации, первый запрос после сжатия.

    Создаётся сверкой корпуса над сессией журнала, условиями его прогона и
    записями эталона; request_lines — строки действительных записей эталона.
    """

    def __init__(
        self,
        session: PromptSession,
        spec: CompactionSpec,
        oracle: SessionLog,
        hasher: ReplayHasher,
        request_lines: Sequence[int],
        borrow_safeguards: bool,
    ) -> None:
        self._session = session
        self._spec = spec
        self._oracle = oracle
        self._hasher = hasher
        self._request_lines = tuple(request_lines)
        self._log = session.log
        self._path = session.journal.path
        self._rebuilder = RequestRebuilder(oracle, hasher)
        self._boundaries = BoundaryVerifier(self._log, self._path, session.model)
        self._stubs = StubVerifier(self._log, self._path)
        self._wire = WireLog(spec.wire_dirs)
        self._debug = DebugLevels(spec.debug_logs, spec.debug_skip)
        self._summary = SummaryWireCheck(session, oracle, hasher, borrow_safeguards)
        self._diff = JsonDiff()

    def run(self) -> tuple[Report, Report]:
        """Частные проверки модели и отдельно — тела запросов суммаризации целиком."""
        report = Report(log=self._path)
        bodies = Report(log=self._path)

        try:
            self._run(report, bodies)
        except (CompactionError, RequestError, ReplayError) as exc:
            raise VerifyError(f"{self._path}: verifying the compaction model failed: {exc}") from exc

        return report, bodies

    def _run(self, report: Report, bodies: Report) -> None:
        attempts = self._verify_checks(report)
        wires = self._wire.compactions()

        for position, boundary in enumerate(self._log.boundaries()):
            attempt = attempts.get(boundary.line)
            if attempt is None:
                attempt = self._boundaries.attempt(boundary, False)

            if isinstance(attempt, LadderStop):
                report.add(f"boundary {boundary.line} ladder", False, f"model stops with {attempt.reason}, journal has a boundary")
                continue

            self._boundaries.verify(boundary, attempt, report)
            wire_position = position - self._spec.wire_skip

            if 0 <= wire_position < len(wires):
                self._summary.compare(attempt, wires[wire_position], boundary, self._log.process_start(boundary.line), report)
                bodies.findings.append(self._summary.whole(attempt, wires[wire_position], boundary))

        self._verify_first_requests(report)
        self._verify_tool_clearing(report)
        self._stubs.verify(report)

    def _verify_tool_clearing(self, report: Report) -> None:
        """Правка clear_tool_uses в context_management каждого запроса против плана модели."""
        mismatches: list[str] = []
        planned = 0
        requests = 0

        for request in self._oracle.requests:
            if request.line not in self._request_lines:
                continue
            requests += 1
            predicted = self._session.clearing.tool_clearing(request.line)
            recorded = self._clearing_edit(self._rebuilder.rebuild(request).body.get("context_management"))
            if predicted is not None:
                planned += 1
            wanted: Json = None
            if predicted is not None:
                wanted = dict(predicted)
            if self._plain(wanted) != self._plain(recorded):
                mismatches.append(f"line {request.line}: model {predicted!r}, request {recorded!r}")

        report.add(f"clear_tool_uses edit in {requests} requests ({planned} planned)", not mismatches, "; ".join(mismatches))

    def _verify_checks(self, report: Report) -> dict[int, Attempt | LadderStop]:
        """Проверки порога по журналу: исход каждой против записанного, уровни против отладочных журналов."""
        attempts: dict[int, Attempt | LadderStop] = {}
        levels: list[str] = []

        for item in self._session.replay(self._request_lines).outcomes():
            check = item.check
            decision = item.decision
            levels.append(str(decision.level))
            recorded = self._recorded(check.terminal, check.boundary is not None, check.line)
            report.add(f"check before line {check.line}: {decision.level} tokens={decision.tokens}", item.outcome == recorded, f"model {item.outcome}, journal {recorded}")

            if isinstance(item.attempt, Attempt) and check.boundary is not None:
                attempts[check.boundary.line] = item.attempt

        if self._spec.debug_logs:
            logged = self._debug.levels()
            if self._spec.debug_tail:
                levels = levels[len(levels) - len(logged) :]
            report.same("levels of checks against the debug logs", levels, logged)

        return attempts

    def _recorded(self, terminal: bool, compacted: bool, line: int) -> str:
        if terminal:
            return f"turn ends: {self._terminal_text(line)}"

        if compacted:
            return "compacted"

        return "request sent"

    def _verify_first_requests(self, report: Report) -> None:
        """Первый запрос после каждой границы: собранные messages и метки против эталона."""
        for boundary in self._log.boundaries():
            for request in self._oracle.requests:
                if request.line < boundary.line or request.line not in self._request_lines:
                    continue
                self._verify_first_request(boundary, request.line, report)
                break

    def _verify_first_request(self, boundary: Message, line: int, report: Report) -> None:
        reference = self._rebuilder.rebuild(self._oracle.request_at(line))
        expected = reference.body.get(Key.MESSAGES)
        assembled = self._session.requests.messages(line, False)
        marked = self._session.requests.parts.marked(assembled.messages, assembled.marks, "")
        detail = self._diff.first(self._marks_only(expected), self._marks_only(marked), "messages")
        same = False
        if isinstance(expected, list):
            same = self._same(expected, assembled.messages)
            if not same:
                detail = self._diff.first(expected, marked, "messages")

        report.add(
            f"boundary {boundary.line}: first request after compaction (line {line}, {len(assembled.messages)} messages)",
            reference.ok and same and detail == "",
            detail[:200],
        )

    def _same(self, expected: Sequence[Json], actual: Sequence[Mapping[str, Json]]) -> bool:
        if len(expected) != len(actual):
            return False

        for left, right in zip(expected, actual):
            if not isinstance(left, dict) or self._hasher.message(left) != self._hasher.message(right):
                return False

        return True

    def _marks_only(self, messages: Json) -> Json:
        """Места меток кэша в messages без их значений: [номер сообщения, номер блока]."""
        places: list[Json] = []
        if not isinstance(messages, list):
            return places

        for position, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            content = message.get(Key.CONTENT)
            if not isinstance(content, list):
                continue
            for index, block in enumerate(content):
                if isinstance(block, dict) and Key.CACHE_CONTROL in block:
                    places.append([position, index])

        return places

    def _plain(self, value: Json) -> str:
        return json.dumps(value, sort_keys=True, default=float)

    def _clearing_edit(self, management: Json) -> Json:
        if not isinstance(management, dict):
            return None

        edits = management.get("edits")
        if not isinstance(edits, list):
            return None

        for edit in edits:
            if isinstance(edit, dict) and edit.get("type") == IdleEdit.TYPE:
                return edit

        return None

    def _terminal_text(self, line: int) -> str:
        for message in self._log.messages:
            if message.line != line:
                continue
            if message.raw.get("apiError") == "autocompact_thrashing":
                return "autocompact_thrashing"
            for block in message.blocks:
                text = block.get("text")
                if isinstance(text, str):
                    return text

        return ""
