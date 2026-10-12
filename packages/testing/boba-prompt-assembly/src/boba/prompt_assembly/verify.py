"""Единая сверка пакета с эталоном: тело запроса целиком, загрузчик, разметка процессов, сжатие.

Для каждого журнала корпуса строит тела запросов из записей истории и
сравнивает контрольную сумму с requestDigest записи эталона; при расхождении
находит первый различающийся путь JSON и считает совпадения по слоям тела.
Отдельно сверяет моменты запросов, найденные по истории, записи, дописанные
возобновлением, начала процессов и модель сжатия (compaction_checks).

Ошибки:
VerifyError — журнал корпуса не открывается либо слой под сверкой сорвался
    целиком; текст называет журнал и причину. Сбой сборки одного запроса
    сверку не срывает: он записывается расхождением этого запроса.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Iterator, Mapping, Sequence

from boba.prompt_assembly.compaction_checks import CompactionVerifier, Report
from boba.prompt_assembly.corpus import CorpusEntry, CorpusManifest, ManifestReader, VerifyError
from boba.prompt_assembly.loader import LoaderError, LoadResult
from boba.prompt_assembly.oracle import BodyLayers, JsonDiff, Layer, ReplayError, ReplayHasher, RequestRebuilder, RequestRecord, SessionLog
from boba.prompt_assembly.records import Json, Key, RecordType, Row, Subtype
from boba.prompt_assembly.request import LaunchParams, RequestError
from boba.prompt_assembly.session import OptionsReader, PromptSession, SessionError, Sessions


class Tally(StrEnum):
    """Счётчики сводки сверки: слои тела, моменты, загрузчик, сжатие."""

    DIGEST = "requestDigest"
    MESSAGES = "messages"
    SYSTEM = "system"
    TOOLS = "tools"
    PARAMS = "params"
    MARKS = "cache marks"
    MOMENTS = "request moments found by history (journal without oracle records)"
    PROCESSES = "process starts found by history"
    APPENDED = "rows appended on resume"
    COMPACTION = "compaction model checks"
    SUMMARIES = "summary request bodies against the wire"


class Link(StrEnum):
    """Поля связи записей, которые читает сверка дописанного возобновлением."""

    LOGICAL_PARENT = "logicalParentUuid"


@dataclass(frozen=True)
class RequestVerdict:
    """Итог сверки одного запроса эталона.

    moment_line — строка момента, найденного по истории (0 — у запроса нет
    ответа в журнале, по истории он не виден); moment_ok — тело, собранное на
    этой строке по журналу без записей эталона, дало ту же контрольную сумму.
    """

    ordinal: int
    line: int
    source: str
    profile: str
    digest_ok: bool
    reference_ok: bool
    layers: Mapping[Layer, bool]
    moment_line: int
    moment_ok: bool
    detail: str
    moment_detail: str
    notes: Sequence[str]

    @property
    def ok(self) -> bool:
        if not self.reference_ok:
            return False

        return self.digest_ok


@dataclass(frozen=True)
class AppendVerdict:
    """Итог сверки записей, дописанных возобновлением, для одного старта процесса."""

    cut: int
    expected: int
    actual: int
    ok: bool
    detail: str


@dataclass(frozen=True)
class ProcessVerdict:
    """Согласие разметки процессов с цепочкой записей эталона на одном запросе."""

    line: int
    ok: bool
    detail: str


@dataclass(frozen=True)
class JournalReport:
    """Итог сверки журнала по всем слоям."""

    entry: CorpusEntry
    requests: Sequence[RequestVerdict]
    unmatched_moments: Sequence[int]
    processes: Sequence[ProcessVerdict]
    appends: Sequence[AppendVerdict]
    compaction: Report | None
    summaries: Report | None
    loader_notes: Sequence[str]


@dataclass(frozen=True)
class Score:
    """Счётчик сводки: сколько сошлось из скольки."""

    matched: int
    total: int

    @property
    def ok(self) -> bool:
        return self.matched == self.total


@dataclass(frozen=True)
class CorpusReport:
    """Итог сверки корпуса: отчёты журналов и счётчики по слоям."""

    journals: Sequence[JournalReport]
    scores: Mapping[Tally, Score]

    @property
    def ok(self) -> bool:
        for score in self.scores.values():
            if not score.ok:
                return False

        return True


class AppendCheck:
    """Сверка записей, дописанных возобновлением, с предсказанием загрузчика.

    Сравнивает число записей, их тип, содержимое сообщения и родителя (uuid
    синтетических записей у модели свои, поэтому родитель сверяется по
    месту), а также родителя первой новой записи — им должна быть последняя
    запись загруженной истории (у границы сжатия, с которой начинается процесс
    /compact, связь лежит в logicalParentUuid).
    """

    def __init__(self, session: PromptSession) -> None:
        self._memory = session.memory
        self._path = session.journal.path

    def run(self, since: int) -> list[AppendVerdict]:
        verdicts: list[AppendVerdict] = []
        for cut in self._memory.cuts():
            if cut > since:
                verdicts.append(self._check(cut))

        return verdicts

    def notes(self, since: int) -> list[str]:
        notes: list[str] = []
        for cut in self._memory.cuts():
            if cut <= since:
                continue
            for note in self._loaded(cut).notes:
                notes.append(f"resume at line {cut}: {note}")

        return notes

    def _loaded(self, cut: int) -> LoadResult:
        try:
            return self._memory.loaded(cut)
        except LoaderError as exc:
            raise VerifyError(f"{self._path}: loading the journal state before line {cut} failed: {exc}") from exc

    def _check(self, cut: int) -> AppendVerdict:
        result = self._loaded(cut)
        actual = self._memory.persisted(cut)
        expected = result.appended
        problems: list[str] = []

        if len(actual) != len(expected):
            problems.append(f"model appends {len(expected)} row(s), binary appended {len(actual)}")

        renamed: dict[str, str] = {}
        for model, record in zip(expected, actual):
            renamed[model.uuid] = record.uuid
            problems.extend(self._differences(model, record, renamed))

        problems.extend(self._prompt_parent(cut, result, renamed))
        return AppendVerdict(
            cut=cut, expected=len(expected), actual=len(actual), ok=not problems, detail="; ".join(problems)
        )

    def _differences(self, model: Row, record: Row, renamed: Mapping[str, str]) -> Iterator[str]:
        if model.kind != record.kind:
            yield f"line {record.line}: model row is {model.kind}, binary wrote {record.kind}"

        if model.content != record.content:
            yield f"line {record.line}: message content differs"

        parent = self._parent(model)
        if renamed.get(parent, parent) != self._parent(record):
            yield f"line {record.line}: model parent {parent}, binary parent {self._parent(record)}"

    def _prompt_parent(self, cut: int, result: LoadResult, renamed: Mapping[str, str]) -> Iterator[str]:
        prompt = self._memory.first_own(cut)
        if prompt is None or not result.history:
            return

        expected = result.next_parent
        actual = self._parent(prompt)
        if prompt.kind == RecordType.SYSTEM and prompt.subtype == Subtype.COMPACT_BOUNDARY:
            actual = str(prompt.raw.get(Link.LOGICAL_PARENT))

        if renamed.get(expected, expected) != actual:
            yield f"line {prompt.line}: the first new row hangs off {actual}, model expects parent {expected}"

    def _parent(self, row: Row) -> str:
        parent = row.parent
        if parent is None:
            return ""

        return parent


class ProcessCheck:
    """Сверка разметки процессов по истории с цепочкой записей эталона.

    Запись эталона с keep 0 без base начинает цепочку заново: так бывает у
    первого запроса журнала, у первого запроса нового процесса и после
    сжатия. Значит, между запросами одной цепочки начала процесса быть не
    должно, а перед началом новой цепочки должно найтись начало процесса
    либо граница сжатия.
    """

    def __init__(self, session: PromptSession, oracle: SessionLog) -> None:
        self._memory = session.memory
        self._oracle = oracle
        self._boundaries: list[int] = []

        for row in session.memory.rows:
            if row.kind == RecordType.SYSTEM and row.subtype == Subtype.COMPACT_BOUNDARY:
                self._boundaries.append(row.line)

    def run(self, since: int) -> list[ProcessVerdict]:
        verdicts: list[ProcessVerdict] = []
        previous = 0

        for request in self._oracle.requests:
            if request.line > since:
                verdicts.append(self._verdict(request, previous))
            previous = request.line

        return verdicts

    def _verdict(self, request: RequestRecord, previous: int) -> ProcessVerdict:
        started = self._between(self._memory.cuts(), previous, request.line)
        compacted = self._between(self._boundaries, previous, request.line)
        restarted = request.keep == 0 and not request.base

        if not restarted and started:
            return ProcessVerdict(request.line, False, f"the model starts a process at line {started[-1]} inside one api-request chain")

        if restarted and previous and not started and not compacted:
            return ProcessVerdict(
                request.line, False, f"the api-request chain restarts after line {previous}, the model finds neither a process start nor a boundary"
            )

        return ProcessVerdict(request.line, True, "")

    def _between(self, lines: Sequence[int], after: int, upto: int) -> list[int]:
        found: list[int] = []
        for line in lines:
            if after < line <= upto:
                found.append(line)

        return found


class RequestCheck:
    """Сверка запросов одного журнала: тело из истории против эталона, целиком и по слоям.

    Параметры запуска берутся из манифеста; для запросов, у которых манифест
    называет другой набор, строится вариант сессии над тем же журналом.
    Параметр safeguards, которого в истории нет, берётся из эталона, когда
    манифест это разрешает, и это отмечается в имени набора.
    """

    def __init__(
        self, session: PromptSession, entry: CorpusEntry, variants: Mapping[int, LaunchParams], oracle: SessionLog, hasher: ReplayHasher
    ) -> None:
        self._session = session
        self._entry = entry
        self._variants = variants
        self._oracle = oracle
        self._hasher = hasher
        self._rebuilder = RequestRebuilder(oracle, hasher)
        self._layers = BodyLayers(hasher)
        self._diff = JsonDiff()
        self._sessions = Sessions()

        try:
            self._bare = self._sessions.without_oracle(session)
        except SessionError as exc:
            raise VerifyError(f"{session.journal.path}: opening a session without the oracle records failed: {exc}") from exc

    def verdicts(self) -> list[RequestVerdict]:
        verdicts: list[RequestVerdict] = []
        requests = self._oracle.requests

        for position, request in enumerate(requests):
            if request.line <= self._entry.valid_after:
                continue
            following = 0
            if position + 1 < len(requests):
                following = requests[position + 1].line
            verdicts.append(self._verdict(position + 1, request, following))

        return verdicts

    def unmatched_moments(self) -> list[int]:
        """Моменты по истории после действительной строки, которым не нашлось записи эталона."""
        covered: set[int] = set()
        requests = self._oracle.requests

        for position, request in enumerate(requests):
            following = 0
            if position + 1 < len(requests):
                following = requests[position + 1].line
            covered.add(self._moment_line(request.line, following))

        unmatched: list[int] = []
        for moment in self._session.memory.moments():
            if moment.line > self._entry.valid_after and moment.line not in covered:
                unmatched.append(moment.line)

        return unmatched

    def _verdict(self, ordinal: int, request: RequestRecord, following: int) -> RequestVerdict:
        try:
            rebuilt = self._rebuilder.rebuild(request)
        except ReplayError as exc:
            raise VerifyError(f"{self._session.journal.path}: rebuilding the oracle request at line {request.line} failed: {exc}") from exc

        reference = rebuilt.body
        profile, launch = self._launch_for(request.line, reference)
        session = self._variant(self._session, launch, request.line)
        moment_line = self._moment_line(request.line, following)
        failed: dict[Layer, bool] = {}
        for layer in Layer:
            failed[layer] = False

        try:
            built = session.requests.build(request.line)
        except RequestError as exc:
            return RequestVerdict(
                ordinal, request.line, request.query_source, profile, False, rebuilt.ok, failed, moment_line, False,
                f"build failed: {exc}", "", (),
            )

        layers: dict[Layer, bool] = {}
        for layer in Layer:
            layers[layer] = self._layers.same(layer, reference, built.body)

        digest_ok = self._hasher.request(built.body) == request.digest
        detail = ""
        if not digest_ok:
            detail = self._diff.first(self._layers.normalized(reference), self._layers.normalized(built.body), "")

        moment_detail = "the request got no response, history does not show it"
        if moment_line:
            moment_detail = self._moment(self._variant(self._bare, launch, request.line), moment_line, request.digest)

        return RequestVerdict(
            ordinal, request.line, request.query_source, profile, digest_ok, rebuilt.ok, layers, moment_line,
            moment_line > 0 and moment_detail == "", detail, moment_detail, built.notes,
        )

    def _moment(self, session: PromptSession, line: int, digest: str) -> str:
        """Пусто, если тело, собранное на строке момента по истории, дало ту же контрольную сумму."""
        try:
            built = session.requests.build(line)
        except RequestError as exc:
            return f"build at history moment line {line} failed: {exc}"

        if self._hasher.request(built.body) == digest:
            return ""

        return f"the body built at history moment line {line} has another digest"

    def _moment_line(self, line: int, following: int) -> int:
        """Строка момента по истории для записи эталона; 0 — до следующей записи ответа нет."""
        moment = self._session.memory.moment_for(line)
        if moment is None:
            return 0

        if following and moment.line > following:
            return 0

        return moment.line

    def _launch_for(self, line: int, reference: Mapping[str, Json]) -> tuple[str, LaunchParams]:
        """Имя набора и параметры запуска запроса: общие для журнала либо свои для этой строки."""
        launch = self._session.options.launch
        profile = self._entry.launch.profile

        if line in self._variants:
            launch = replace(self._variants[line], tool_clearing_mode=launch.tool_clearing_mode)
            profile = self._entry.launch.request_profiles[line]

        safeguards = reference.get(Key.SAFEGUARDS)
        if self._entry.launch.safeguards_from_oracle and isinstance(safeguards, list):
            launch = replace(launch, safeguards=tuple(safeguards))
            profile = f"{profile} (safeguards taken from the reference)"

        return profile, launch

    def _variant(self, session: PromptSession, launch: LaunchParams, line: int) -> PromptSession:
        if launch is session.options.launch:
            return session

        try:
            return self._sessions.variant(session, launch)
        except SessionError as exc:
            raise VerifyError(f"{self._session.journal.path}: opening a session for the request at line {line} failed: {exc}") from exc


class JournalVerifier:
    """Сверка одного журнала корпуса по всем слоям; создаётся CorpusVerifier."""

    def __init__(self, manifest: CorpusManifest, reader: ManifestReader) -> None:
        self._manifest = manifest
        self._reader = reader
        self._options = OptionsReader()
        self._sessions = Sessions()
        self._hasher = ReplayHasher()

    def verify(self, entry: CorpusEntry) -> JournalReport:
        try:
            session = self._sessions.open(entry.path, self._reader.options(self._manifest, entry, entry.launch.profile))
            oracle = SessionLog(session.journal, self._hasher)
            oracle.load()
        except (SessionError, ReplayError) as exc:
            raise VerifyError(f"opening corpus journal {entry.address.label} failed: {exc}") from exc

        check = RequestCheck(session, entry, self._variants(entry), oracle, self._hasher)
        appends = AppendCheck(session)
        compaction: Report | None = None
        summaries: Report | None = None

        if entry.compaction is not None:
            valid: list[int] = []
            for request in oracle.requests:
                if request.line > entry.valid_after:
                    valid.append(request.line)
            verifier = CompactionVerifier(
                session, entry.compaction, oracle, self._hasher, valid, entry.launch.safeguards_from_oracle
            )
            compaction, summaries = verifier.run()

        return JournalReport(
            entry=entry,
            requests=check.verdicts(),
            unmatched_moments=check.unmatched_moments(),
            processes=ProcessCheck(session, oracle).run(entry.valid_after),
            appends=appends.run(entry.valid_after),
            compaction=compaction,
            summaries=summaries,
            loader_notes=appends.notes(entry.valid_after),
        )

    def _variants(self, entry: CorpusEntry) -> dict[int, LaunchParams]:
        variants: dict[int, LaunchParams] = {}

        for line, profile in entry.launch.request_profiles.items():
            try:
                variants[line] = self._options.profile(profile).launch
            except SessionError as exc:
                raise VerifyError(f"{self._manifest.path}: journal {entry.address.label}, request at line {line}: {exc}") from exc

        return variants


class CorpusVerifier:
    """Сверка списка журналов корпуса и сводные счётчики по слоям.

    Создаётся входом командной строки над манифестом; печать отчёта — дело
    входа.
    """

    def __init__(self, manifest: CorpusManifest, reader: ManifestReader) -> None:
        self._journals = JournalVerifier(manifest, reader)

    def run(self, entries: Sequence[CorpusEntry]) -> CorpusReport:
        reports: list[JournalReport] = []
        for entry in entries:
            reports.append(self._journals.verify(entry))

        return CorpusReport(journals=tuple(reports), scores=self._scores(reports))

    def _scores(self, reports: Sequence[JournalReport]) -> dict[Tally, Score]:
        verdicts: list[RequestVerdict] = []
        for report in reports:
            verdicts.extend(report.requests)

        layers = {
            Tally.MESSAGES: Layer.MESSAGES,
            Tally.SYSTEM: Layer.SYSTEM,
            Tally.TOOLS: Layer.TOOLS,
            Tally.PARAMS: Layer.PARAMS,
            Tally.MARKS: Layer.MARKS,
        }
        scores: dict[Tally, Score] = {Tally.DIGEST: self._score(self._flags_ok(verdicts))}
        for tally, layer in layers.items():
            scores[tally] = self._score(self._flags_layer(verdicts, layer))

        scores[Tally.MOMENTS] = self._score(self._flags_moments(verdicts))
        scores[Tally.PROCESSES] = self._score(self._flags_processes(reports))
        scores[Tally.APPENDED] = self._score(self._flags_appends(reports))
        scores[Tally.COMPACTION] = self._score(self._flags_findings(reports, False))
        scores[Tally.SUMMARIES] = self._score(self._flags_findings(reports, True))
        return scores

    def _score(self, flags: Iterator[bool]) -> Score:
        matched = 0
        total = 0
        for flag in flags:
            total += 1
            if flag:
                matched += 1

        return Score(matched=matched, total=total)

    def _flags_ok(self, verdicts: Sequence[RequestVerdict]) -> Iterator[bool]:
        for verdict in verdicts:
            yield verdict.ok

    def _flags_layer(self, verdicts: Sequence[RequestVerdict], layer: Layer) -> Iterator[bool]:
        for verdict in verdicts:
            yield verdict.layers[layer]

    def _flags_moments(self, verdicts: Sequence[RequestVerdict]) -> Iterator[bool]:
        """Запросы, на которые в журнале есть ответ: момент по истории дал то же тело."""
        for verdict in verdicts:
            if verdict.moment_line:
                yield verdict.moment_ok

    def _flags_processes(self, reports: Sequence[JournalReport]) -> Iterator[bool]:
        for report in reports:
            for process in report.processes:
                yield process.ok

    def _flags_appends(self, reports: Sequence[JournalReport]) -> Iterator[bool]:
        for report in reports:
            for append in report.appends:
                yield append.ok

    def _flags_findings(self, reports: Sequence[JournalReport], summaries: bool) -> Iterator[bool]:
        for report in reports:
            chosen = report.compaction
            if summaries:
                chosen = report.summaries
            if chosen is None:
                continue
            for finding in chosen.findings:
                yield finding.ok
