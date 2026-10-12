"""Эталон: восстановление тел запросов из записей api-request журнала (Claude Code 2.1.289).

Читает записи api-request-shape / api-request-blob / api-request, собирает
полное тело каждого запроса и сверяет его с контрольными суммами записи.
Здесь же каноническая сериализация и хеши клиента (IC/Cke @207529623), которыми
сверка считает requestDigest собранного тела.

Ошибки:
ReplayError — запрос не найден или запись запроса не разбирается; текст
    называет журнал, строку и причину.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from boba.prompt_assembly.records import Encoding, Journal, JsNumber, Json, Key, RecordType

Value = None | bool | int | float | str | Sequence["Value"] | Mapping[str, "Value"]



class ReplayError(Exception):
    """Единственная ошибка инструмента, выходящая наружу."""


class Source(StrEnum):
    BLOB = "blob"
    JOURNAL = "journal"
    MISSING = "missing"


class Text(StrEnum):
    JSON_SUFFIX = ".json"


class CanonicalJson:
    """Каноническая сериализация IC/Cke из бинарника.

    Ключи объекта сортируются по кодовым единицам UTF-16, разделителей и
    пробелов нет, примитивы пишутся как JSON.stringify. С omit_cache_control
    ключ cache_control пропускается на любой глубине. Используется
    ReplayHasher для всех хешей формата.
    """

    _LONE_SURROGATE = re.compile("[\ud800-\udfff]")

    def render(self, value: Json, omit_cache_control: bool) -> str:
        parts: list[str] = []
        self._emit(value, parts, omit_cache_control)
        return "".join(parts)

    def _emit(self, value: Json, parts: list[str], omit: bool) -> None:
        if isinstance(value, dict):
            self._emit_object(value, parts, omit)
            return

        if isinstance(value, list):
            self._emit_array(value, parts, omit)
            return

        parts.append(self._primitive(value))

    def _emit_array(self, items: Sequence[Json], parts: list[str], omit: bool) -> None:
        parts.append("[")

        for position, item in enumerate(items):
            if position > 0:
                parts.append(",")
            self._emit(item, parts, omit)

        parts.append("]")

    def _emit_object(self, obj: Mapping[str, Json], parts: list[str], omit: bool) -> None:
        parts.append("{")
        first = True

        for key in sorted(obj, key=self._utf16_order):
            if omit and key == Key.CACHE_CONTROL:
                continue

            if not first:
                parts.append(",")

            first = False
            parts.append(self._string(key))
            parts.append(":")
            self._emit(obj[key], parts, omit)

        parts.append("}")

    def _primitive(self, value: Json) -> str:
        if value is None:
            return "null"

        if value is True:
            return "true"

        if value is False:
            return "false"

        if isinstance(value, str):
            return self._string(value)

        if isinstance(value, JsNumber):
            return value.literal

        if isinstance(value, int):
            return str(value)

        raise ReplayError(f"canonical json: unsupported value {value!r} of type {type(value).__name__}")

    def _string(self, value: str) -> str:
        dumped = json.dumps(value, ensure_ascii=False)
        return self._LONE_SURROGATE.sub(self._escape_surrogate, dumped)

    @staticmethod
    def _escape_surrogate(match: re.Match[str]) -> str:
        return f"\\u{ord(match.group(0)):04x}"

    @staticmethod
    def _utf16_order(key: str) -> bytes:
        return key.encode(Encoding.UTF16_BE, Encoding.SURROGATES)


class ReplayHasher:
    """Хеши формата: сообщение (Ake), shape, дайджест запроса (Mke).

    Собирает канонический текст через CanonicalJson и считает sha256 от его
    UTF-8. Им пользуются JournalIndex (хеши прогонов журнала) и
    RequestRebuilder (проверка восстановленного).
    """

    _DIGEST_EXCLUDED = frozenset(("metadata", "stream", "thread", "diagnostics"))

    def __init__(self) -> None:
        self._canonical = CanonicalJson()

    def message(self, message: Mapping[str, Json]) -> str:
        normalized = self.normalized(message)
        return self._sha256(self._canonical.render(normalized, True))

    def shape(self, shape: Mapping[str, Json]) -> str:
        return self._sha256(self._canonical.render(dict(shape), True))

    def request(self, body: Mapping[str, Json]) -> str:
        digested: dict[str, Json] = {}

        for key, value in body.items():
            if key in self._DIGEST_EXCLUDED:
                continue
            digested[key] = self._digest_value(key, value)

        return self._sha256(self._canonical.render(digested, False))

    def normalized(self, message: Mapping[str, Json]) -> dict[str, Json]:
        content = message.get("content")

        if not isinstance(content, str):
            return dict(message)

        normalized = dict(message)
        normalized["content"] = [{"type": "text", "text": content}]
        return normalized

    def _digest_value(self, key: str, value: Json) -> Json:
        if key != Key.MESSAGES:
            return value

        if not isinstance(value, list):
            return value

        messages: list[Json] = []
        for message in value:
            if not isinstance(message, dict):
                raise ReplayError(f"request digest: message expected as an object, got {type(message).__name__}")
            messages.append(self.normalized(message))

        return messages

    def _sha256(self, text: str) -> str:
        return hashlib.sha256(text.encode(Encoding.UTF8, Encoding.SURROGATES)).hexdigest()


@dataclass(frozen=True)
class LogLine:
    """Разобранная строка журнала с её номером (с единицы)."""

    number: int
    record: Mapping[str, Json]

    @property
    def kind(self) -> str:
        kind = self.record.get("type")
        if isinstance(kind, str):
            return kind
        return ""


@dataclass(frozen=True)
class StoredMessage:
    """Сообщение запроса без меток кэша и место, откуда оно взято."""

    message: dict[str, Json]
    source: Source
    line: int


@dataclass
class _Run:
    role: str
    message_id: str | None
    content: list[Json]


class JournalIndex:
    """Повтор индекса q9 писателя: какие хеши сообщений уже лежат в журнале.

    Писатель не пишет blob для сообщения, хеш которого совпал с хешем
    прогона обычных записей user/assistant; здесь тот же прогон строится
    заново и для каждого хеша запоминается само сообщение. Наполняется
    SessionLog построчно, читается RequestRebuilder.
    """

    def __init__(self, hasher: ReplayHasher) -> None:
        self._hasher = hasher
        self._messages: dict[str, StoredMessage] = {}
        self._run: _Run | None = None
        self.has_fork_context_ref = False

    def find(self, digest: str) -> StoredMessage | None:
        return self._messages.get(digest)

    def note(self, line: LogLine) -> None:
        kind = line.kind

        if kind == RecordType.FORK_REF:
            self.has_fork_context_ref = True
            return

        if kind == RecordType.BLOB:
            self._note_blob(line)
            return

        if kind != RecordType.USER and kind != RecordType.ASSISTANT:
            return

        self._note_transcript(line)

    def _note_blob(self, line: LogLine) -> None:
        digest = line.record.get("hash")
        message = line.record.get("message")

        if not isinstance(digest, str):
            return

        if not isinstance(message, dict):
            return

        self._remember(digest, StoredMessage(message, Source.BLOB, line.number))

    def _note_transcript(self, line: LogLine) -> None:
        message = line.record.get("message")

        if not isinstance(message, dict):
            self._run = None
            return

        role = message.get("role")
        if not isinstance(role, str):
            self._run = None
            return

        if "content" not in message:
            self._run = None
            return

        message_id = message.get("id")
        if not isinstance(message_id, str):
            message_id = None

        blocks = self._blocks(message["content"])
        content = blocks
        if self._continues(role, message_id):
            content = self._previous_content() + blocks

        self._run = _Run(role, message_id, content)
        restored: dict[str, Json] = {"role": role, "content": self._without_cache_control(content)}
        digest = self._hasher.message(restored)
        self._remember(digest, StoredMessage(restored, Source.JOURNAL, line.number))

    def _continues(self, role: str, message_id: str | None) -> bool:
        run = self._run

        if run is None:
            return False

        if run.role != role:
            return False

        if role == "user":
            return True

        if message_id is None:
            return False

        return run.message_id == message_id

    def _previous_content(self) -> list[Json]:
        if self._run is None:
            return []
        return self._run.content

    def _blocks(self, content: Json) -> list[Json]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}]

        if isinstance(content, list):
            return list(content)

        return [content]

    def _without_cache_control(self, value: Json) -> Json:
        if isinstance(value, list):
            items: list[Json] = []
            for item in value:
                items.append(self._without_cache_control(item))
            return items

        if isinstance(value, dict):
            cleaned: dict[str, Json] = {}
            for key, item in value.items():
                if key == Key.CACHE_CONTROL:
                    continue
                cleaned[key] = self._without_cache_control(item)
            return cleaned

        return value

    def _remember(self, digest: str, stored: StoredMessage) -> None:
        if digest in self._messages:
            return
        self._messages[digest] = stored


@dataclass(frozen=True)
class CacheMark:
    """Метка кэша: путь к объекту в теле запроса и значение cache_control."""

    path: Sequence[str | int]
    value: Json


@dataclass(frozen=True)
class RequestRecord:
    """Запись api-request, разобранная один раз на входе."""

    line: int
    id: str
    session_id: str
    agent_id: str
    timestamp: str
    version: str
    query_source: str
    shape_hash: str
    params: Mapping[str, Json]
    withheld: Sequence[str]
    base: str
    keep: int
    tail: Sequence[str]
    cache_control: Sequence[CacheMark]
    digest: str
    is_cut: bool
    cut: Json


class RequestRecords:
    """Разбор сырой записи api-request в RequestRecord.

    Живёт в SessionLog; обязательные поля записи проверяются здесь, дальше
    код работает только с моделью.
    """

    def parse(self, line: LogLine, path: Path) -> RequestRecord:
        record = line.record
        where = f"{path}:{line.number}: api-request"
        cut_digest = record.get("cutRequestDigest")
        is_cut = isinstance(cut_digest, str)
        digest_key = "requestDigest"
        if is_cut:
            digest_key = "cutRequestDigest"

        params = record.get("params")
        if not isinstance(params, dict):
            raise ReplayError(f"{where}: expected params as an object, got {type(params).__name__}")

        keep = record.get("keep")
        if not isinstance(keep, int):
            raise ReplayError(f"{where}: expected keep as an integer, got {keep!r}")

        return RequestRecord(
            line=line.number,
            id=self._text(record, "id", where),
            session_id=self._text(record, "sessionId", where),
            agent_id=self._optional_text(record, "agentId"),
            timestamp=self._text(record, "timestamp", where),
            version=self._text(record, "version", where),
            query_source=self._text(record, "querySource", where),
            shape_hash=self._text(record, "shapeHash", where),
            params=params,
            withheld=tuple(self._strings(record.get("withheld", []), "withheld", where)),
            base=self._optional_text(record, "base"),
            keep=keep,
            tail=tuple(self._strings(record.get("tail"), "tail", where)),
            cache_control=tuple(self._marks(record.get("cacheControl"), where)),
            digest=self._text(record, digest_key, where),
            is_cut=is_cut,
            cut=record.get("cut"),
        )

    def _text(self, record: Mapping[str, Json], key: str, where: str) -> str:
        value = record.get(key)
        if not isinstance(value, str):
            raise ReplayError(f"{where}: expected {key} as a string, got {value!r}")
        return value

    def _optional_text(self, record: Mapping[str, Json], key: str) -> str:
        value = record.get(key)
        if not isinstance(value, str):
            return ""
        return value

    def _strings(self, value: Json, key: str, where: str) -> Iterator[str]:
        if not isinstance(value, list):
            raise ReplayError(f"{where}: expected {key} as an array of strings, got {type(value).__name__}")

        for item in value:
            if not isinstance(item, str):
                raise ReplayError(f"{where}: expected {key} items as strings, got {item!r}")
            yield item

    def _marks(self, value: Json, where: str) -> Iterator[CacheMark]:
        if not isinstance(value, list):
            raise ReplayError(f"{where}: expected cacheControl as an array, got {type(value).__name__}")

        for item in value:
            if not isinstance(item, dict):
                raise ReplayError(f"{where}: expected a cacheControl item as an object, got {item!r}")
            yield CacheMark(tuple(self._path(item.get("path"), where)), item.get("value"))

    def _path(self, value: Json, where: str) -> Iterator[str | int]:
        if not isinstance(value, list):
            raise ReplayError(f"{where}: expected cacheControl path as an array, got {value!r}")

        for step in value:
            if isinstance(step, bool):
                raise ReplayError(f"{where}: expected a cacheControl path step as a string or an index, got {step!r}")
            if not isinstance(step, (str, int)):
                raise ReplayError(f"{where}: expected a cacheControl path step as a string or an index, got {step!r}")
            yield step


class SessionLog:
    """Журнал сессии, прочитанный целиком: shape-записи, запросы, индекс сообщений.

    Создаётся над Journal сверкой и командой oracle; строки, которые не
    разбираются как JSON-объект, журнал пропускает так же, как это делает
    писатель (jxr). Отдаёт данные RequestRebuilder.
    """

    def __init__(self, journal: Journal, hasher: ReplayHasher) -> None:
        self._journal = journal
        self._path = journal.path
        self._records = RequestRecords()
        self.index = JournalIndex(hasher)
        self.shapes: dict[str, Mapping[str, Json]] = {}
        self.requests: list[RequestRecord] = []
        self.by_id: dict[str, RequestRecord] = {}
        self.skipped_lines = 0

    def load(self) -> None:
        self.skipped_lines = self._journal.unparsed()

        for number, record in self._journal.entries():
            self._note(number, record)

    def request_at(self, line: int) -> RequestRecord:
        for request in self.requests:
            if request.line == line:
                return request

        raise ReplayError(f"{self._path}:{line}: an api-request record is expected at this line, found none")

    def _note(self, number: int, record: Mapping[str, Json]) -> None:
        line = LogLine(number, record)
        self.index.note(line)

        if line.kind == RecordType.SHAPE:
            self._note_shape(line)
            return

        if line.kind == RecordType.REQUEST:
            request = self._records.parse(line, self._path)
            self.requests.append(request)
            self.by_id[request.id] = request

    def _note_shape(self, line: LogLine) -> None:
        digest = line.record.get("shapeHash")
        shape = line.record.get("shape")

        if not isinstance(digest, str):
            return

        if not isinstance(shape, dict):
            return

        if digest in self.shapes:
            return

        self.shapes[digest] = shape


@dataclass(frozen=True)
class MessageCheck:
    """Итог по одному сообщению запроса: откуда взято и сошёлся ли хеш."""

    position: int
    expected: str
    actual: str
    source: Source
    line: int

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


@dataclass
class Rebuilt:
    """Восстановленный запрос: тело и результаты всех проверок."""

    request: RequestRecord
    body: dict[str, Json]
    messages: list[MessageCheck]
    shape_found: bool
    shape_hash_actual: str
    digest_actual: str
    problems: list[str] = field(default_factory=lambda: list[str]())

    @property
    def shape_ok(self) -> bool:
        return self.shape_found and self.shape_hash_actual == self.request.shape_hash

    @property
    def messages_ok(self) -> bool:
        for check in self.messages:
            if not check.ok:
                return False

        return True

    @property
    def digest_ok(self) -> bool:
        return self.digest_actual == self.request.digest

    @property
    def ok(self) -> bool:
        if self.problems:
            return False

        if not self.shape_ok:
            return False

        if not self.messages_ok:
            return False

        return self.digest_ok

    def report(self) -> dict[str, Json]:
        checks: list[Json] = []
        for check in self.messages:
            checks.append(
                {
                    "index": check.position,
                    "hash": check.expected,
                    "recomputed": check.actual,
                    "source": str(check.source),
                    "line": check.line,
                    "ok": check.ok,
                }
            )

        digest_field = "requestDigest"
        if self.request.is_cut:
            digest_field = "cutRequestDigest"

        return {
            "id": self.request.id,
            "line": self.request.line,
            "timestamp": self.request.timestamp,
            "querySource": self.request.query_source,
            "agentId": self.request.agent_id,
            "base": self.request.base,
            "keep": self.request.keep,
            "tail": list(self.request.tail),
            "withheld": list(self.request.withheld),
            "cut": self.request.cut,
            "shapeHash": self.request.shape_hash,
            "shapeHashRecomputed": self.shape_hash_actual,
            "shapeOk": self.shape_ok,
            "messages": checks,
            "messagesOk": self.messages_ok,
            "digestField": digest_field,
            "digest": self.request.digest,
            "digestRecomputed": self.digest_actual,
            "digestOk": self.digest_ok,
            "problems": list(self.problems),
            "ok": self.ok,
        }


class RequestRebuilder:
    """Сборка полного тела запроса по записи api-request и её проверка.

    Раскручивает цепочку base/keep/tail в список хешей, достаёт сообщения
    из SessionLog (blob или прогон журнала), подставляет shape, расставляет
    метки кэша и пересчитывает хеши через ReplayHasher. Создаётся сверкой и командой oracle.
    """

    def __init__(self, log: SessionLog, hasher: ReplayHasher) -> None:
        self._log = log
        self._hasher = hasher
        self._hashes: dict[str, Sequence[str]] = {}

    def rebuild(self, request: RequestRecord) -> Rebuilt:
        problems: list[str] = []
        hashes = self._chain(request, problems)
        messages: list[Json] = []
        checks: list[MessageCheck] = []

        for position, digest in enumerate(hashes):
            check = self._restore(position, digest, request, messages, problems)
            checks.append(check)

        body: dict[str, Json] = {}
        self._put(body, Key.MODEL, request.params.get(Key.MODEL))
        shape = self._log.shapes.get(request.shape_hash)
        shape_hash_actual = ""

        if shape is None:
            problems.append(f"shape {request.shape_hash} has no api-request-shape record")

        if shape is not None:
            shape_hash_actual = self._hasher.shape(shape)
            self._put(body, Key.SYSTEM, self._copy(shape.get(Key.SYSTEM)))
            self._put(body, Key.TOOLS, self._copy(shape.get(Key.TOOLS)))

        body[Key.MESSAGES] = messages

        for key, value in request.params.items():
            if key == Key.MODEL:
                continue
            body[key] = self._copy(value)

        for mark in request.cache_control:
            self._mark(body, mark, problems)

        return Rebuilt(
            request=request,
            body=body,
            messages=checks,
            shape_found=shape is not None,
            shape_hash_actual=shape_hash_actual,
            digest_actual=self._hasher.request(body),
            problems=problems,
        )

    def _chain(self, request: RequestRecord, problems: list[str]) -> Sequence[str]:
        if cached := self._hashes.get(request.id):
            return cached

        kept: Sequence[str] = ()
        if request.keep > 0:
            kept = self._kept(request, problems)

        hashes = tuple(kept) + tuple(request.tail)
        self._hashes[request.id] = hashes
        return hashes

    def _kept(self, request: RequestRecord, problems: list[str]) -> Sequence[str]:
        base = self._log.by_id.get(request.base)

        if base is None:
            problems.append(f"base request {request.base!r} is not in the log, first {request.keep} messages are lost")
            return ()

        if base.line >= request.line:
            problems.append(f"base request {request.base} is written at line {base.line}, after the request itself")
            return ()

        base_hashes = self._chain(base, problems)

        if len(base_hashes) < request.keep:
            problems.append(f"base request {request.base} has {len(base_hashes)} messages, keep expects {request.keep}")
            return ()

        return base_hashes[: request.keep]

    def _restore(
        self,
        position: int,
        digest: str,
        request: RequestRecord,
        messages: list[Json],
        problems: list[str],
    ) -> MessageCheck:
        stored = self._log.index.find(digest)

        if stored is None:
            problems.append(f"message {position} with hash {digest} is neither a blob nor a journal run")
            messages.append({"role": "unknown", "content": []})
            return MessageCheck(position, digest, "", Source.MISSING, 0)

        if stored.line > request.line:
            problems.append(f"message {position} source at line {stored.line} is written after the request")

        message = self._copy(stored.message)
        messages.append(message)
        return MessageCheck(position, digest, self._hasher.message(stored.message), stored.source, stored.line)

    def _mark(self, body: dict[str, Json], mark: CacheMark, problems: list[str]) -> None:
        target: Json = body

        for step in mark.path:
            target = self._step(target, step)

        if not isinstance(target, dict):
            problems.append(f"cache_control path {list(mark.path)} does not lead to an object")
            return

        target[Key.CACHE_CONTROL] = self._copy(mark.value)

    def _step(self, target: Json, step: str | int) -> Json:
        if isinstance(target, dict) and isinstance(step, str):
            return target.get(step)

        if not isinstance(target, list):
            return None

        if not isinstance(step, int):
            return None

        if step >= len(target):
            return None

        return target[step]

    def _put(self, body: dict[str, Json], key: str, value: Json) -> None:
        if value is None:
            return
        body[key] = value

    def _copy(self, value: Json) -> Json:
        if isinstance(value, list):
            items: list[Json] = []
            for item in value:
                items.append(self._copy(item))
            return items

        if isinstance(value, dict):
            copied: dict[str, Json] = {}
            for key, item in value.items():
                copied[key] = self._copy(item)
            return copied

        return value


class Layer(StrEnum):
    """Слои тела запроса, по которым сверка ведёт отдельные счётчики."""

    MESSAGES = "messages"
    SYSTEM = "system"
    TOOLS = "tools"
    PARAMS = "params"
    MARKS = "marks"


class JsonDiff:
    """Первый различающийся путь между двумя значениями JSON."""

    def first(self, expected: Value, actual: Value, path: str) -> str:
        if isinstance(expected, Mapping) and isinstance(actual, Mapping):
            return self._objects(expected, actual, path)

        if self._array(expected) and self._array(actual):
            return self._arrays(self._items(expected), self._items(actual), path)

        if expected == actual and isinstance(expected, bool) == isinstance(actual, bool):
            return ""

        return f"{path}: expected {self._show(expected)}, got {self._show(actual)}"

    def _objects(self, expected: Mapping[str, Value], actual: Mapping[str, Value], path: str) -> str:
        for key in expected:
            if key not in actual:
                return f"{path}/{key}: expected {self._show(expected[key])}, got no such key"

        for key in actual:
            if key not in expected:
                return f"{path}/{key}: expected no such key, got {self._show(actual[key])}"

        for key in expected:
            if found := self.first(expected[key], actual[key], f"{path}/{key}"):
                return found

        return ""

    def _arrays(self, expected: Sequence[Value], actual: Sequence[Value], path: str) -> str:
        for index, (left, right) in enumerate(zip(expected, actual)):
            if found := self.first(left, right, f"{path}/{index}"):
                return found

        if len(expected) != len(actual):
            return f"{path}: expected {len(expected)} items, got {len(actual)}"

        return ""

    def _array(self, value: Value) -> bool:
        if isinstance(value, (str, Mapping)):
            return False

        return isinstance(value, Sequence)

    def _items(self, value: Value) -> Sequence[Value]:
        if isinstance(value, (str, Mapping)) or not isinstance(value, Sequence):
            return ()

        return value

    def _show(self, value: Value) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > 240:
            return f"{text[:240]}… ({len(text)} chars)"

        return text


class BodyLayers:
    """Сравнение собранного тела с эталонным по слоям и приведение к сравнимому виду.

    Слой messages сравнивается хешами сообщений (без меток кэша), слой marks —
    местами и значениями меток в messages, system и tools — целиком, params —
    все остальные ключи тела. Создаётся сверкой.
    """

    def __init__(self, hasher: ReplayHasher) -> None:
        self._hasher = hasher
        self._canonical = CanonicalJson()

    def same(self, layer: Layer, expected: Mapping[str, Json], actual: Mapping[str, Json]) -> bool:
        if layer is Layer.MESSAGES:
            return self._hashes(expected) == self._hashes(actual)

        if layer is Layer.MARKS:
            return list(self._marks(self.normalized(expected).get(Key.MESSAGES), ())) == list(
                self._marks(self.normalized(actual).get(Key.MESSAGES), ())
            )

        if layer in (Layer.SYSTEM, Layer.TOOLS):
            return self._text(expected.get(layer.value)) == self._text(actual.get(layer.value))

        return self._text(self._params(expected)) == self._text(self._params(actual))

    def normalized(self, body: Mapping[str, Json]) -> dict[str, Json]:
        """Тело, в котором строковое содержимое сообщений развёрнуто в блоки, как при подсчёте хеша."""
        result = dict(body)
        messages = body.get(Key.MESSAGES)
        if not isinstance(messages, list):
            return result

        normalized: list[Json] = []
        for message in messages:
            if isinstance(message, dict):
                normalized.append(self._hasher.normalized(message))

        result[Key.MESSAGES] = normalized
        return result

    def _hashes(self, body: Mapping[str, Json]) -> list[str]:
        hashes: list[str] = []
        messages = body.get(Key.MESSAGES)
        if not isinstance(messages, list):
            return hashes

        for message in messages:
            if isinstance(message, dict):
                hashes.append(self._hasher.message(message))

        return hashes

    def _marks(self, value: Json, path: tuple[str | int, ...]) -> Iterator[tuple[tuple[str | int, ...], str]]:
        if isinstance(value, dict):
            if Key.CACHE_CONTROL in value:
                yield path, self._text(value[Key.CACHE_CONTROL])
            for key, item in value.items():
                yield from self._marks(item, (*path, str(key)))

        if isinstance(value, list):
            for index, item in enumerate(value):
                yield from self._marks(item, (*path, index))

    def _params(self, body: Mapping[str, Json]) -> dict[str, Json]:
        params: dict[str, Json] = {}
        for key, value in body.items():
            if key in (Key.MESSAGES, Key.SYSTEM, Key.TOOLS):
                continue
            params[str(key)] = value

        return params

    def _text(self, value: Json) -> str:
        return self._canonical.render(value, False)
