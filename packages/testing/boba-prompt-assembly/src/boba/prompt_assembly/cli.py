"""Вход командной строки пакета: build, history, compaction, oracle, verify.

Разбирает аргументы в модели и зовёт операции слоёв; логики сборки здесь
нет. Внешние параметры запуска читаются только здесь — из файла, именованного
набора и явных ключей.

Ошибки:
CliError — аргументы противоречат друг другу, результат не записывается либо
    слой под командой сорвался; текст называет команду, файл и причину.
    Ошибки слоёв (SessionError, RequestError, CompactionError, LoaderError,
    ReplayError, VerifyError, JournalError) приходят упакованными в неё.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Sequence

from boba.prompt_assembly.compaction import Attempt, CheckOutcome, CompactionError, LadderStop
from boba.prompt_assembly.compaction_checks import Report
from boba.prompt_assembly.corpus import CorpusEntry, CorpusManifest, LaunchSpec, ManifestReader, VerifyError
from boba.prompt_assembly.loader import LoaderError, LoadResult, RowKit, SessionLoader
from boba.prompt_assembly.oracle import Rebuilt, ReplayError, ReplayHasher, RequestRebuilder, SessionLog, Source
from boba.prompt_assembly.records import (
    Encoding,
    Journal,
    JournalAddress,
    JournalAddresses,
    JournalError,
    Json,
    Key,
    RecordType,
    Row,
)
from boba.prompt_assembly.request import BuiltRequest, LaunchProfile, RequestError
from boba.prompt_assembly.session import OptionsReader, PromptSession, SessionError, SessionOptions, Sessions
from boba.prompt_assembly.verify import CorpusReport, CorpusVerifier, JournalReport, RequestVerdict, Tally


class CliError(Exception):
    """Команда не выполнена."""


class Command(StrEnum):
    BUILD = "build"
    HISTORY = "history"
    COMPACTION = "compaction"
    ORACLE = "oracle"
    VERIFY = "verify"


class Exit(StrEnum):
    """Тексты и имена, общие для команд."""

    PROGRAM = "prompt_assembly"
    JSON_SUFFIX = ".json"
    ASSIGN = "="
    OK = "OK"
    FAIL = "FAIL"


@dataclass(frozen=True)
class LaunchArgs:
    """Аргументы команд, задающие внешние параметры запуска."""

    params: Path | None
    profile: str
    overrides: Sequence[str]


class Output:
    """Печать результата команды: в stdout либо в файл."""

    def __init__(self, target: Path | None) -> None:
        self._target = target

    def document(self, document: Json) -> None:
        self.text(json.dumps(document, ensure_ascii=False, indent=1))

    def text(self, text: str) -> None:
        if self._target is None:
            sys.stdout.write(f"{text}\n")
            return

        try:
            self._target.write_bytes(text.encode(Encoding.UTF8, Encoding.SURROGATES))
        except OSError as exc:
            raise CliError(f"writing the result to {self._target} failed: {exc}") from exc


class LaunchOptions:
    """Внешние параметры запуска из аргументов команды: набор, файл, явные ключи.

    Без набора и файла параметров нет вовсе — тогда ошибка перечисляет всё,
    чего в журнале не найти.
    """

    def __init__(self, args: LaunchArgs) -> None:
        self._args = args
        self._reader = OptionsReader()

    def read(self) -> SessionOptions:
        document: dict[str, Json] = {}
        where = "command line"

        if self._args.params is not None:
            where = str(self._args.params)
            try:
                document.update(self._reader.file(self._args.params))
            except SessionError as exc:
                raise CliError(str(exc)) from exc

        if self._args.profile:
            document["profile"] = self._args.profile

        for item in self._args.overrides:
            key, value = self._override(item)
            document[key] = value

        try:
            return self._reader.read(document, where)
        except SessionError as exc:
            raise CliError(str(exc)) from exc

    def _override(self, item: str) -> tuple[str, Json]:
        key, found, raw = item.partition(Exit.ASSIGN)
        if not found or not key:
            raise CliError(f"--set expects KEY=JSON, got {item!r}")

        try:
            value: Json = json.loads(raw)
        except ValueError as exc:
            raise CliError(f"--set {key}: the value is expected to be JSON (quote strings), got {raw!r}: {exc}") from exc

        return key, value


class BuildCommand:
    """build: тело запроса (или всех запросов) журнала в JSON."""

    def __init__(self, args: argparse.Namespace, options: SessionOptions) -> None:
        self._journal: Path = args.journal
        self._at: int | None = args.at
        self._request: int | None = args.request
        self._body_only: bool = args.body_only
        self._output = Output(args.out)
        self._options = options

    def run(self) -> int:
        try:
            session = Sessions().open(self._journal, self._options)
            built = list(self._built(session))
        except (SessionError, RequestError) as exc:
            raise CliError(f"build {self._journal}: {exc}") from exc

        documents: list[Json] = []
        for item in built:
            documents.append(self._document(item))

        if self._at is not None or self._request is not None:
            self._output.document(documents[0])
            return 0

        self._output.document(documents)
        return 0

    def _built(self, session: PromptSession) -> Iterator[BuiltRequest]:
        if self._at is not None:
            yield session.requests.build(self._at)
            return

        moments = session.memory.moments()
        if self._request is None:
            for moment in moments:
                yield session.requests.build(moment.line)
            return

        if self._request < 1 or self._request > len(moments):
            raise CliError(
                f"build {self._journal}: --request expects a number from 1 to {len(moments)} "
                f"(request moments found by history), got {self._request}"
            )

        yield session.requests.build(moments[self._request - 1].line)

    def _document(self, item: BuiltRequest) -> Json:
        if self._body_only:
            return item.body

        notes: list[Json] = []
        notes.extend(item.notes)
        return {"line": item.line, "body": item.body, "notes": notes}


class HistoryCommand:
    """history: восстановленная цепочка — что загрузчик оставил, что снял, что дописал бы."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._path: Path = args.journal
        self._upto: int | None = args.upto
        self._as_json: bool = args.json
        self._output = Output(args.out)
        self._kit = RowKit()

    def run(self) -> int:
        try:
            journal = Journal(self._path)
            journal.load()
            result = SessionLoader(journal).load(self._upto)
        except (JournalError, LoaderError) as exc:
            raise CliError(f"history {self._path}: {exc}") from exc

        dropped = list(self._dropped(journal, result))
        if self._as_json:
            self._output.document(self._document(result, dropped))
            return 0

        self._output.text("\n".join(self._lines(journal, result, dropped)))
        return 0

    def _dropped(self, journal: Journal, result: LoadResult) -> Iterator[Row]:
        """Строки истории файла, которых нет в загруженном списке."""
        kept: set[str] = set()
        for row in result.history:
            kept.add(row.uuid)

        seen: set[str] = set()
        for number, raw in journal.entries(self._upto):
            row = Row(line=number, raw=raw)
            if row.kind not in (RecordType.USER, RecordType.ASSISTANT, RecordType.ATTACHMENT, RecordType.SYSTEM):
                continue
            if row.uuid in kept or row.uuid in seen:
                continue
            seen.add(row.uuid)
            yield row

    def _lines(self, journal: Journal, result: LoadResult, dropped: Sequence[Row]) -> Iterator[str]:
        state = "the whole file"
        if self._upto is not None:
            state = f"line {self._upto}"

        yield f"journal: {journal.path} ({journal.line_count} lines, {result.skipped_lines} unparsed)"
        yield f"state as of {state}: found={result.found} leaf={result.leaf} turn={result.turn.value}"
        yield f"kept by the loader: {len(result.history) - len(result.appended)} row(s)"
        for row in result.history:
            if row.line:
                yield f"  kept     {self._describe(row)}"

        yield f"dropped by the loader: {len(dropped)} row(s)"
        for row in dropped:
            yield f"  dropped  {self._describe(row)}"

        yield f"appended on resume: {len(result.appended)} row(s); the new prompt hangs off {result.next_parent}"
        for row in result.appended:
            yield f"  appended parent={row.parent} {self._describe(row)}"

        yield f"metadata repeated at the tail: {', '.join(result.reappended)}"
        for note in result.notes:
            yield f"note: {note}"

    def _describe(self, row: Row) -> str:
        detail = row.attachment_kind
        if row.kind == RecordType.SYSTEM:
            detail = row.subtype
        if row.kind in (RecordType.USER, RecordType.ASSISTANT):
            detail = self._content(row)

        return f"line={row.line:<4} {row.kind:<10} {row.uuid[:8]} {detail}"

    def _content(self, row: Row) -> str:
        content = row.content
        if isinstance(content, str):
            return f"text:{content[:40]!r}"

        parts: list[str] = []
        for block in row.blocks:
            parts.append(str(block.get(Key.TYPE)))

        return " ".join(parts)

    def _document(self, result: LoadResult, dropped: Sequence[Row]) -> Json:
        history: list[Json] = []
        for row in result.history:
            history.append({"line": row.line, "uuid": row.uuid, "type": row.kind, "parentUuid": row.parent})

        removed: list[Json] = []
        for row in dropped:
            removed.append({"line": row.line, "uuid": row.uuid, "type": row.kind})

        appended: list[Json] = []
        for row in result.appended:
            appended.append(dict(row.raw))

        notes: list[Json] = list(result.notes)
        reappended: list[Json] = list(result.reappended)
        return {
            "found": result.found,
            "leaf": result.leaf,
            "nextParent": result.next_parent,
            "turn": result.turn.value,
            "skippedLines": result.skipped_lines,
            "history": history,
            "dropped": removed,
            "appended": appended,
            "reappended": reappended,
            "notes": notes,
        }


class CompactionCommand:
    """compaction: заполненность перед каждым запросом, порог, решение, границы и хвост."""

    def __init__(self, args: argparse.Namespace, options: SessionOptions) -> None:
        self._journal: Path = args.journal
        self._output = Output(args.out)
        self._options = options

    def run(self) -> int:
        try:
            session = Sessions().open(self._journal, self._options)
            outcomes = session.replay(session.request_lines()).outcomes()
        except (SessionError, CompactionError) as exc:
            raise CliError(f"compaction {self._journal}: {exc}") from exc

        self._output.text("\n".join(self._lines(session, outcomes)))
        return 0

    def _lines(self, session: PromptSession, outcomes: Sequence[CheckOutcome]) -> Iterator[str]:
        policy = session.policy
        yield (
            f"window {policy.window} ({policy.source.value}), effective {policy.effective_window}, "
            f"threshold {policy.threshold}, blocking limit {policy.blocking_limit}"
        )

        for item in outcomes:
            fill = item.fill
            yield (
                f"before line {item.check.line}: fill {item.decision.tokens} "
                f"(usage anchor {fill.anchor_tokens} + tail estimate {fill.tail_tokens} + server cleared {fill.server_cleared}), "
                f"level {item.decision.level.value}, decision {item.decision.kind.value} -> {item.outcome}"
            )
            yield from self._attempt(item)

        for boundary in session.log.boundaries():
            metadata = boundary.metadata
            yield (
                f"boundary at line {boundary.line}: trigger {metadata.get('trigger')}, preTokens {metadata.get('preTokens')}, "
                f"postTokens {metadata.get('postTokens')}"
            )

    def _attempt(self, item: CheckOutcome) -> Iterator[str]:
        attempt = item.attempt
        if isinstance(attempt, LadderStop):
            yield f"    nothing to summarize: {attempt.reason.value} ({attempt.total_groups} group(s))"
            return

        if not isinstance(attempt, Attempt):
            return

        kept: list[str] = []
        for message in attempt.to_keep:
            kept.append(str(message.line))

        recorded = "no boundary in the journal"
        if item.check.boundary is not None:
            recorded = f"boundary recorded at line {item.check.boundary.line}"

        yield (
            f"    split {attempt.split.value}: {attempt.total_groups} group(s), {len(attempt.to_summarize)} row(s) summarized, "
            f"tail kept verbatim: lines {', '.join(kept)}; {recorded}"
        )


class OracleCommand:
    """oracle: декодер эталона — тела запросов из записей api-request и сверка их контрольных сумм."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._path: Path = args.journal
        self._request: str = args.request
        self._all_dir: Path | None = args.all_dir
        self._body_only: bool = args.body_only
        self._output = Output(args.out)
        self._hasher = ReplayHasher()

    def run(self) -> int:
        try:
            journal = Journal(self._path)
            journal.load()
            log = SessionLog(journal, self._hasher)
            log.load()
            rebuilder = RequestRebuilder(log, self._hasher)
            rebuilt: list[Rebuilt] = []
            for request in log.requests:
                rebuilt.append(rebuilder.rebuild(request))
        except (JournalError, ReplayError) as exc:
            raise CliError(f"oracle {self._path}: {exc}") from exc

        if self._request:
            item = self._select(rebuilt)
            self._output.text(self._document(item))
            return self._code(item.ok)

        if self._all_dir is not None:
            self._dump_all(rebuilt, self._all_dir)

        failed = 0
        lines: list[str] = [f"journal: {self._path}", f"requests: {len(rebuilt)}, shapes: {len(log.shapes)}, unparsed lines: {log.skipped_lines}"]
        for ordinal, item in enumerate(rebuilt, start=1):
            if not item.ok:
                failed += 1
            lines.extend(self._item(ordinal, item))

        lines.append(f"result: {len(rebuilt) - failed} ok, {failed} failed")
        self._output.text("\n".join(lines))
        return self._code(failed == 0)

    def _item(self, ordinal: int, item: Rebuilt) -> Iterator[str]:
        request = item.request
        blobs = 0
        journal = 0
        for check in item.messages:
            if check.source is Source.BLOB:
                blobs += 1
            if check.source is Source.JOURNAL:
                journal += 1

        base = "-"
        if request.base:
            base = request.base[:8]

        digest_name = "digest"
        if request.is_cut:
            digest_name = "cutDigest"

        yield (
            f"#{ordinal} line={request.line} id={request.id[:8]} src={request.query_source} base={base} "
            f"keep={request.keep} tail={len(request.tail)} messages={len(item.messages)} "
            f"(blob={blobs} journal={journal}) marks={len(request.cache_control)} "
            f"shape={self._flag(item.shape_ok)} hashes={self._flag(item.messages_ok)} "
            f"{digest_name}={self._flag(item.digest_ok)}"
        )

        for problem in item.problems:
            yield f"    problem: {problem}"

        if not item.digest_ok:
            yield f"    digest: expected {request.digest}, got {item.digest_actual}"

    def _select(self, rebuilt: Sequence[Rebuilt]) -> Rebuilt:
        wanted = self._request
        if wanted.isdigit():
            ordinal = int(wanted)
            if ordinal < 1 or ordinal > len(rebuilt):
                raise CliError(f"oracle {self._path}: request #{ordinal} requested, the journal has {len(rebuilt)} requests")
            return rebuilt[ordinal - 1]

        matched: list[Rebuilt] = []
        for item in rebuilt:
            if item.request.id.startswith(wanted):
                matched.append(item)

        if len(matched) != 1:
            raise CliError(f"oracle {self._path}: request id {wanted!r} is expected to match one request, matched {len(matched)}")

        return matched[0]

    def _dump_all(self, rebuilt: Sequence[Rebuilt], directory: Path) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CliError(f"oracle {self._path}: creating output directory {directory} failed: {exc}") from exc

        for ordinal, item in enumerate(rebuilt, start=1):
            Output(directory / f"{ordinal:04d}_{item.request.id}{Exit.JSON_SUFFIX}").text(self._document(item))

    def _document(self, item: Rebuilt) -> str:
        document: Json = {"check": item.report(), "body": item.body}
        if self._body_only:
            document = item.body

        return json.dumps(document, ensure_ascii=False, indent=2)

    def _flag(self, ok: bool) -> str:
        if ok:
            return Exit.OK

        return Exit.FAIL

    def _code(self, ok: bool) -> int:
        if ok:
            return 0

        return 1


class VerifyCommand:
    """verify: сверка построенного с эталоном по корпусу либо по перечисленным журналам."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._corpus: Path | None = args.corpus
        self._journals: Sequence[Path] = args.journals
        self._profile: str = args.profile
        self._all: bool = args.all_findings
        self._notes: bool = args.notes
        self._output = Output(args.out)
        self._reader = ManifestReader()
        self._addresses = JournalAddresses()
        self._options = OptionsReader()

    def run(self) -> int:
        try:
            manifest, entries = self._targets()
            report = CorpusVerifier(manifest, self._reader).run(entries)
        except (VerifyError, JournalError) as exc:
            raise CliError(f"verify: {exc}") from exc

        self._output.text("\n".join(self._lines(report)))
        if report.ok:
            return 0

        return 1

    def _targets(self) -> tuple[CorpusManifest, list[CorpusEntry]]:
        if self._corpus is not None:
            manifest = self._reader.read(self._corpus)
            wanted: list[JournalAddress] = []
            for path in self._journals:
                wanted.append(self._addresses.of_path(path))

            entries = self._reader.journals(manifest, wanted)
            known: set[JournalAddress] = set()
            for entry in entries:
                known.add(entry.address)

            for path in self._journals:
                if self._addresses.of_path(path) not in known:
                    entries.append(self._adhoc(path))

            return manifest, entries

        if not self._journals:
            raise CliError("verify expects --corpus MANIFEST.json or at least one journal, got neither")

        limits = self._options.default_compaction()
        manifest = CorpusManifest(path=Path("."), projects=Path("."), limits=limits, tool_clearing_mode="unknown", entries=())
        adhoc: list[CorpusEntry] = []
        for path in self._journals:
            adhoc.append(self._adhoc(path))

        return manifest, adhoc

    def _adhoc(self, path: Path) -> CorpusEntry:
        """Журнал вне манифеста: все записи эталона действительны, параметры — из набора --profile."""
        launch = LaunchSpec(profile=self._profile, echo_api_block_order=False, safeguards_from_oracle=False, request_profiles={})
        return CorpusEntry(
            address=self._addresses.of_path(path),
            path=path,
            scenario="given on the command line",
            valid_after=0,
            launch=launch,
            compaction=None,
            loader_case="",
        )

    def _lines(self, report: CorpusReport) -> Iterator[str]:
        for journal in report.journals:
            yield from self._journal(journal)

        yield "summary:"
        for tally in Tally:
            score = report.scores[tally]
            yield f"  {tally.value}: {score.matched} of {score.total} {self._flag(score.ok)}"

        verdict = "every layer matched"
        if not report.ok:
            verdict = "MISMATCHES FOUND"
        yield f"result: {verdict}"

    def _journal(self, journal: JournalReport) -> Iterator[str]:
        matched = 0
        for verdict in journal.requests:
            if verdict.ok:
                matched += 1

        entry = journal.entry
        yield f"== {entry.address.label}"
        yield f"   {entry.scenario[:200]}"
        yield f"   requests after line {entry.valid_after}: {len(journal.requests)}, digest matched: {matched}"

        for verdict in journal.requests:
            yield from self._request(verdict)

        if journal.unmatched_moments:
            yield f"   history moments without an oracle record (lines): {list(journal.unmatched_moments)}"

        for process in journal.processes:
            if not process.ok:
                yield f"   process marks at request line {process.line}: {Exit.FAIL} {process.detail}"

        for append in journal.appends:
            if append.ok and not self._all:
                continue
            yield (
                f"   resume at line {append.cut}: appended rows model={append.expected} binary={append.actual} "
                f"{self._flag(append.ok)} {append.detail}"
            )

        yield from self._findings("compaction", journal.compaction)
        yield from self._findings("summary bodies", journal.summaries)

        if self._notes:
            for note in journal.loader_notes:
                yield f"      note: {note}"

    def _request(self, verdict: RequestVerdict) -> Iterator[str]:
        settled = verdict.moment_ok or not verdict.moment_line
        if verdict.ok and settled and not self._all:
            return

        layers: list[str] = []
        for layer, same in verdict.layers.items():
            layers.append(f"{layer.value}={self._flag(same)}")

        moment = "no history moment (the request got no response)"
        if verdict.moment_line:
            moment = f"moment line {verdict.moment_line} {self._flag(verdict.moment_ok)}"

        yield (
            f"   #{verdict.ordinal} line={verdict.line} src={verdict.source} digest={self._flag(verdict.digest_ok)} "
            f"{' '.join(layers)} reference={self._flag(verdict.reference_ok)} {moment} profile={verdict.profile}"
        )
        if verdict.detail:
            yield f"      first difference: {verdict.detail}"

        if verdict.moment_detail and verdict.moment_line:
            yield f"      history moment: {verdict.moment_detail}"

        if self._notes:
            for note in verdict.notes:
                yield f"      note: {note}"

    def _findings(self, title: str, report: Report | None) -> Iterator[str]:
        if report is None or not report.findings:
            return

        yield f"   {title}: {len(report.findings) - report.failed} of {len(report.findings)} checks passed"
        for finding in report.findings:
            if finding.ok and not self._all:
                continue
            yield f"      {self._flag(finding.ok)} {finding.subject}: {finding.detail[:420]}"

    def _flag(self, ok: bool) -> str:
        if ok:
            return Exit.OK

        return Exit.FAIL


class Cli:
    """Разбор аргументов и запуск команды."""

    def __init__(self, argv: Sequence[str]) -> None:
        parser = argparse.ArgumentParser(
            prog=Exit.PROGRAM, description="Rebuild Claude Code 2.1.289 API requests from a session journal"
        )
        commands = parser.add_subparsers(dest="command", required=True)

        build = commands.add_parser(Command.BUILD, help="build request bodies from the journal history")
        build.add_argument("journal", type=Path, help="session journal (.jsonl)")
        selector = build.add_mutually_exclusive_group()
        selector.add_argument("--at", type=int, help="build the request whose history is the rows before this line")
        selector.add_argument("--request", type=int, help="1-based number of a request moment found by history")
        selector.add_argument("--all", action="store_true", help="every request moment found by history (the default)")
        build.add_argument("--body-only", action="store_true", help="print bodies without source notes")
        self._launch_arguments(build)

        history = commands.add_parser(Command.HISTORY, help="show what --resume would load from the journal")
        history.add_argument("journal", type=Path, help="session journal (.jsonl)")
        history.add_argument("--upto", type=int, help="load the file state as of this line (inclusive); default is the whole file")
        history.add_argument("--json", action="store_true", help="print the result as JSON")
        history.add_argument("--out", type=Path, help="write the result to this file instead of stdout")

        compaction = commands.add_parser(Command.COMPACTION, help="context fill and auto-compaction decisions")
        compaction.add_argument("journal", type=Path, help="session journal (.jsonl)")
        self._launch_arguments(compaction)

        oracle = commands.add_parser(Command.ORACLE, help="decode recorded api-request records (the reference)")
        oracle.add_argument("journal", type=Path, help="session journal (.jsonl)")
        oracle.add_argument("--request", default="", help="request id (or its prefix), or 1-based ordinal number")
        oracle.add_argument("--all", type=Path, dest="all_dir", help="write every request into this directory")
        oracle.add_argument("--body-only", action="store_true", help="write only the body, without the check report")
        oracle.add_argument("--out", type=Path, help="write the result to this file instead of stdout")

        verify = commands.add_parser(Command.VERIFY, help="compare built requests with the recorded reference")
        verify.add_argument("journals", type=Path, nargs="*", help="journals to verify; default is the whole corpus")
        verify.add_argument("--corpus", type=Path, help="corpus manifest (MANIFEST.json)")
        verify.add_argument(
            "--profile", default=LaunchProfile.SUBSCRIPTION_PRINT.value, help="launch profile for journals outside the manifest"
        )
        verify.add_argument("--all-findings", action="store_true", help="print passed checks as well")
        verify.add_argument("--notes", action="store_true", help="print builder and loader notes")
        verify.add_argument("--out", type=Path, help="write the report to this file instead of stdout")

        self._args = parser.parse_args(argv)

    def run(self) -> int:
        command = Command(self._args.command)

        if command is Command.BUILD:
            return BuildCommand(self._args, self._options()).run()

        if command is Command.HISTORY:
            return HistoryCommand(self._args).run()

        if command is Command.COMPACTION:
            return CompactionCommand(self._args, self._options()).run()

        if command is Command.ORACLE:
            return OracleCommand(self._args).run()

        return VerifyCommand(self._args).run()

    def _options(self) -> SessionOptions:
        args = LaunchArgs(params=self._args.params, profile=self._args.profile, overrides=tuple(self._args.set))
        return LaunchOptions(args).read()

    def _launch_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--params", type=Path, help="JSON file with launch parameters the journal does not record")
        parser.add_argument("--profile", default="", help="named set of launch parameters, e.g. subscription_print")
        parser.add_argument(
            "--set", action="append", default=[], metavar="KEY=JSON", help="override one launch parameter, e.g. main_cache_ttl='\"\"'"
        )
        parser.add_argument("--out", type=Path, help="write the result to this file instead of stdout")


def main() -> None:
    try:
        code = Cli(sys.argv[1:]).run()
    except CliError as exc:
        raise SystemExit(f"{Exit.PROGRAM}: {exc}") from exc

    raise SystemExit(code)


def cli() -> None:
    main()
