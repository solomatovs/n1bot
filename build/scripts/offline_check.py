"""Проверка сборки закрытого контура: белый список реестров, прокси, ловушка DNS, отчёт.

Цель make offline-check собирает всё, кроме fetch, без выхода в сеть. Шаги сборки живут
в сетевом пространстве контейнера-ловушки (команда jail, сеть docker none): в нём есть
только loopback. На 127.0.0.1:53 ловушка записывает каждое имя, которое шаг разрешает
сам, мимо прокси, и отвечает «нет такого»; на 127.0.0.1:<порт> она передаёт байты через
unix-сокет общего каталога прокси (команда proxy) в контейнере с сетью. Прокси пускает
только хосты реестров, которые команда hosts собирает из тех же источников, что и
сборка: build/conf/pip.conf, build/conf/uv.toml, sources.list и реестр npm образов.
Вывод сборки проходит через stamp: он отмечает цель make и шаг Dockerfile по времени,
и report сводит журналы в вердикт с целью, стадией и хостом каждого нарушения.

Вызов:
  offline_check.py hosts --pip-conf <файл> --uv-toml <файл> --apt-image <образ> ...
                         --npm-image <образ> ... --out <allow.jsonl>
  offline_check.py proxy --allow <allow.jsonl> --log <proxy.jsonl> --socket <сокет>
  offline_check.py jail --log <dns.jsonl> --socket <сокет> --port <порт>
  offline_check.py stamp --marks <marks.jsonl>
  offline_check.py report --allow <allow.jsonl> --proxy <proxy.jsonl> --dns <dns.jsonl>
                          --marks <marks.jsonl>

Команды proxy и jail идут в контейнере образа python без зависимостей: только stdlib.

Ошибки:
OfflineCheckError — конфиг индексов или журнал не читается, образ не отдал sources.list
  или реестр npm; печатается текстом, код выхода 2.
Нарушения, найденные отчётом, — не ошибка: report печатает их и выходит с кодом 1.
"""

import argparse
import asyncio
import configparser
import contextlib
import json
import re
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit


class OfflineCheckError(Exception):
    """Проверка не может идти: вход не читается или нарушен формат журнала."""


class Registry(StrEnum):
    PYPI = "pypi"
    APT = "apt"
    NPM = "npm"


class PypiDefault(StrEnum):
    """Индекс pypi, которым pip и uv пользуются без index-url, и хост его файлов."""

    INDEX = "https://pypi.org/simple"
    FILES = "https://files.pythonhosted.org"


class Verdict(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UPSTREAM_FAILED = "upstream-failed"


class MarkKind(StrEnum):
    TARGET = "target"
    HEADER = "header"
    STEP = "step"


class MarkPrefix(StrEnum):
    """Строки вывода сборки, которые отмечает stamp: цель проверки и заголовок make."""

    TARGET = "== offline-check: make "
    HEADER = "== "


class Exit(IntEnum):
    OK = 0
    VIOLATIONS = 1
    ERROR = 2


class DnsCode(IntEnum):
    """Поля DNS-пакета, которые ловушка читает и пишет."""

    HEADER_SIZE = 12
    FLAG_RESPONSE = 0x8000
    FLAG_RECURSION_DESIRED = 0x0100
    FLAG_RECURSION_AVAILABLE = 0x0080
    RCODE_NXDOMAIN = 3
    PORT = 53


class HttpReply(StrEnum):
    ESTABLISHED = "HTTP/1.1 200 Connection established\r\n\r\n"
    STATUS = (
        "HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nConnection: close\r\n"
        "Content-Length: {length}\r\n\r\n"
    )


@dataclass(frozen=True)
class AllowedHost:
    """Хост белого списка: реестр и место конфигурации, откуда он взят."""

    host: str
    registry: Registry
    source: str


@dataclass(frozen=True)
class ProxyEvent:
    """Запрос через прокси: кто, к какому хосту, что решил прокси."""

    time: float
    method: str
    host: str
    port: int
    verdict: Verdict
    detail: str


@dataclass(frozen=True)
class DnsEvent:
    """Имя, которое шаг сборки разрешал сам, мимо прокси."""

    time: float
    name: str


@dataclass(frozen=True)
class Mark:
    """Отметка вывода сборки: цель make, заголовок рецепта или шаг Dockerfile."""

    time: float
    kind: MarkKind
    text: str


@dataclass(frozen=True)
class Place:
    """Где шла сборка в момент события: цель проверки, заголовок make, стадия и шаг."""

    STEP_WIDTH = 120

    target: str
    header: str
    stage: str
    step: str

    def render(self) -> str:
        parts = [f"make {self.target}"]
        if self.header:
            parts.append(self.header)

        if self.stage:
            parts.append(f"stage {self.stage}")

        if self.step:
            step = " ".join(self.step.split())
            parts.append(step[: self.STEP_WIDTH])

        return " / ".join(parts)


class Journal:
    """Журнал событий строками JSON: прокси, ловушка и stamp пишут, report читает."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: Mapping[str, object]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def records(self) -> Iterator[Mapping[str, object]]:
        if not self._path.exists():
            return

        with self._path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                yield self._parse(line, number)

    def _parse(self, line: str, number: int) -> Mapping[str, object]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OfflineCheckError(
                f"{self._path}:{number}: expected a JSON object per line, "
                f"got {line[:200]!r}: {exc}"
            ) from exc

        if not isinstance(record, dict):
            raise OfflineCheckError(
                f"{self._path}:{number}: expected a JSON object, got {line[:200]!r}"
            )

        return record


class Fields:
    """Поля записи журнала с проверкой типа: битая запись падает с файлом и полем."""

    def __init__(self, record: Mapping[str, object], origin: Path) -> None:
        self._record = record
        self._origin = origin

    def text(self, name: str) -> str:
        value = self._record.get(name)
        if not isinstance(value, str):
            raise OfflineCheckError(
                f"{self._origin}: field {name} expects a string, "
                f"got {value!r} in {dict(self._record)}"
            )

        return value

    def number(self, name: str) -> float:
        value = self._record.get(name)
        if not isinstance(value, (int, float)):
            raise OfflineCheckError(
                f"{self._origin}: field {name} expects a number, "
                f"got {value!r} in {dict(self._record)}"
            )

        return float(value)


class EventReader:
    """Журналы проверки в модели: белый список, события прокси и ловушки, отметки."""

    def allowed(self, path: Path) -> list[AllowedHost]:
        return list(self._allowed(path))

    def proxy(self, path: Path) -> list[ProxyEvent]:
        return list(self._proxy(path))

    def dns(self, path: Path) -> list[DnsEvent]:
        return list(self._dns(path))

    def marks(self, path: Path) -> list[Mark]:
        return list(self._marks(path))

    def _allowed(self, path: Path) -> Iterator[AllowedHost]:
        for record in Journal(path).records():
            fields = Fields(record, path)
            yield AllowedHost(
                fields.text("host"),
                Registry(fields.text("registry")),
                fields.text("source"),
            )

    def _proxy(self, path: Path) -> Iterator[ProxyEvent]:
        for record in Journal(path).records():
            fields = Fields(record, path)
            port = int(fields.number("port"))
            verdict = Verdict(fields.text("verdict"))
            yield ProxyEvent(
                fields.number("time"),
                fields.text("method"),
                fields.text("host"),
                port,
                verdict,
                fields.text("detail"),
            )

    def _dns(self, path: Path) -> Iterator[DnsEvent]:
        for record in Journal(path).records():
            fields = Fields(record, path)
            yield DnsEvent(fields.number("time"), fields.text("name"))

    def _marks(self, path: Path) -> Iterator[Mark]:
        for record in Journal(path).records():
            fields = Fields(record, path)
            yield Mark(
                fields.number("time"),
                MarkKind(fields.text("kind")),
                fields.text("text"),
            )


class IndexConfig:
    """Индексы pypi сборки из build/conf/pip.conf (pip) и build/conf/uv.toml (uv).

    Файлы — те самые, что стадия site-conf Dockerfile кладёт в /etc/pip.conf и
    /etc/uv/uv.toml. Файла нет или в нём не назван основной индекс — инструмент идёт
    в pypi по умолчанию, и в список попадают хосты pypi.
    """

    PIP_SECTIONS = ("global", "install", "download", "wheel")

    def hosts(self, pip_conf: Path, uv_toml: Path) -> Iterator[AllowedHost]:
        yield from self._pip(pip_conf)
        yield from self._uv(uv_toml)

    def _pip(self, path: Path) -> Iterator[AllowedHost]:
        parser = configparser.RawConfigParser()
        if path.exists():
            self._read_pip(parser, path)

        has_index = False
        for section in self.PIP_SECTIONS:
            if not parser.has_section(section):
                continue

            for key in ("index-url", "extra-index-url", "find-links"):
                if not parser.has_option(section, key):
                    continue

                if key == "index-url":
                    has_index = True

                for url in parser.get(section, key).split():
                    yield from self._url(url, f"{path} [{section}] {key}")

        if has_index:
            return

        yield from self._pypi_default(f"pip default index: no index-url in {path}")

    def _read_pip(self, parser: configparser.RawConfigParser, path: Path) -> None:
        try:
            parser.read(path, encoding="utf-8")
        except configparser.Error as exc:
            raise OfflineCheckError(
                f"reading pip index settings from {path} failed: {exc}"
            ) from exc

    def _uv(self, path: Path) -> Iterator[AllowedHost]:
        settings: Mapping[str, object] = {}
        if path.exists():
            settings = self._read_uv(path)

        has_index = False
        for scope, table in self._uv_tables(settings):
            if url := table.get("index-url"):
                has_index = True
                yield from self._url(str(url), f"{path} {scope}index-url")

            for key in ("extra-index-url", "find-links"):
                for url in self._strings(table.get(key)):
                    yield from self._url(url, f"{path} {scope}{key}")

            for index in self._tables(table.get("index")):
                if index.get("default") is True:
                    has_index = True

                yield from self._url(
                    str(index.get("url")),
                    f"{path} {scope}[[index]] {index.get('name')}",
                )

        if has_index:
            return

        yield from self._pypi_default(
            f"uv default index: no index-url or default [[index]] in {path}"
        )

    def _read_uv(self, path: Path) -> Mapping[str, object]:
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise OfflineCheckError(
                f"reading uv index settings from {path} failed: {exc}"
            ) from exc

    def _uv_tables(
        self, settings: Mapping[str, object]
    ) -> Iterator[tuple[str, Mapping[str, object]]]:
        yield "", settings

        pip = settings.get("pip")
        if isinstance(pip, dict):
            yield "[pip] ", pip

    def _tables(self, value: object) -> Iterator[Mapping[str, object]]:
        if not isinstance(value, list):
            return

        for item in value:
            if isinstance(item, dict):
                yield item

    def _strings(self, value: object) -> Iterator[str]:
        if isinstance(value, str):
            yield value
            return

        if not isinstance(value, list):
            return

        for item in value:
            yield str(item)

    def _pypi_default(self, source: str) -> Iterator[AllowedHost]:
        for url in PypiDefault:
            yield from self._url(url, source)

    def _url(self, url: str, source: str) -> Iterator[AllowedHost]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return

        if parts.hostname is None:
            raise OfflineCheckError(
                f"{source}: expected an index URL with a host, got {url!r}"
            )

        yield AllowedHost(parts.hostname, Registry.PYPI, source)


class ImageConfig:
    """Реестры, которые несут в себе образы-основы: apt-зеркало и реестр npm.

    Образ запускается без сети; читается ровно то, чем пользуются apt-get и npm
    в стадиях сборки.
    """

    APT_LISTS = (
        "cat /etc/apt/sources.list /etc/apt/sources.list.d/*.list "
        "/etc/apt/sources.list.d/*.sources 2>/dev/null; true"
    )
    DEB822_URIS = re.compile(r"^URIs:\s*(.+)$")

    def apt(self, image: str) -> Iterator[AllowedHost]:
        text = self._run(image, "sh", ["-c", self.APT_LISTS])
        for url in self._apt_urls(text):
            yield from self._url(url, Registry.APT, f"{image} apt sources")

    def npm(self, image: str) -> Iterator[AllowedHost]:
        text = self._run(image, "npm", ["config", "get", "registry"])
        yield from self._url(
            text.strip(), Registry.NPM, f"{image} npm config get registry"
        )

    def _apt_urls(self, text: str) -> Iterator[str]:
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if match := self.DEB822_URIS.match(line):
                yield from match.group(1).split()
                continue

            words = line.split()
            if not words:
                continue

            if words[0] not in ("deb", "deb-src"):
                continue

            yield from self._one_line_url(words)

    def _one_line_url(self, words: Sequence[str]) -> Iterator[str]:
        rest = list(words[1:])
        if rest and rest[0].startswith("["):
            while rest and not rest[0].endswith("]"):
                rest.pop(0)

            rest = rest[1:]

        if rest:
            yield rest[0]

    def _url(self, url: str, registry: Registry, source: str) -> Iterator[AllowedHost]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return

        if parts.hostname is None:
            raise OfflineCheckError(
                f"{source}: expected a registry URL with a host, got {url!r}"
            )

        yield AllowedHost(parts.hostname, registry, source)

    def _run(self, image: str, entrypoint: str, args: Sequence[str]) -> str:
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            entrypoint,
            image,
            *args,
        ]
        shown = " ".join(command)

        try:
            done = subprocess.run(
                command, capture_output=True, text=True, check=True, timeout=120
            )
        except subprocess.CalledProcessError as exc:
            raise OfflineCheckError(
                f"reading registry settings of image {image} ({shown}) failed "
                f"with code {exc.returncode}: {exc.stderr.strip()[:500]}"
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OfflineCheckError(
                f"reading registry settings of image {image} ({shown}) failed: {exc}"
            ) from exc

        return done.stdout


class Allowlist:
    """Белый список прокси: хосты реестров, к которым сборке можно обращаться."""

    def __init__(self, hosts: Iterable[AllowedHost]) -> None:
        self._hosts: set[str] = set()
        for allowed in hosts:
            self._hosts.add(allowed.host.lower())

    def allows(self, host: str) -> bool:
        return host.lower() in self._hosts

    def names(self) -> list[str]:
        return sorted(self._hosts)


class RequestHead:
    """Голова HTTP-запроса к прокси: строка запроса и заголовки как есть."""

    def __init__(self, raw: bytes) -> None:
        text = raw.decode("latin-1")
        lines = text.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            raise OfflineCheckError(
                "proxy: expected a request line 'METHOD target HTTP/x', "
                f"got {lines[0][:200]!r}"
            )

        self.method, self.target, self.version = parts
        self.headers = [line for line in lines[1:] if line]

    def authority(self) -> SplitResult:
        return urlsplit(f"//{self.target}")

    def origin_form(self, parts: SplitResult) -> bytes:
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        lines = [f"{self.method} {path} {self.version}"]
        for header in self.headers:
            name = header.split(":", 1)[0].strip().lower()
            if name in ("connection", "proxy-connection", "keep-alive"):
                continue

            lines.append(header)

        lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class Tunnel:
    """Двусторонняя перекачка байтов между двумя потоками до закрытия обеих сторон.

    Её зовут прокси (клиент ↔ реестр) и ловушка (шаг сборки ↔ unix-сокет прокси).
    """

    CHUNK = 65536

    async def join(
        self,
        left: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        right: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    ) -> None:
        await asyncio.gather(
            self._pump(left[0], right[1]), self._pump(right[0], left[1])
        )
        right[1].close()
        left[1].close()

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while chunk := await reader.read(self.CHUNK):
                writer.write(chunk)
                await writer.drain()

            if writer.can_write_eof():
                writer.write_eof()
        except ConnectionError:
            # сброс одной из сторон — конец туннеля; закрывает его join
            return


class ProxyServer:
    """HTTP-прокси с белым списком хостов: CONNECT для https и absolute-form для http.

    Единственный выход наружу у шагов сборки: ловушка передаёт их соединения сюда
    через unix-сокет. Каждый запрос пишется в журнал с решением; хост вне списка
    получает 403 с объяснением, недоступный разрешённый — 502 с причиной.
    """

    HEAD_LIMIT = 65536
    CONNECT_TIMEOUT = 30.0

    def __init__(self, allowlist: Allowlist, journal: Journal) -> None:
        self._allowlist = allowlist
        self._journal = journal
        self._tunnel = Tunnel()

    async def serve(self, endpoint: Path) -> None:
        endpoint.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._client, str(endpoint), limit=self.HEAD_LIMIT
        )
        allowed = " ".join(self._allowlist.names())
        print(
            f"offline-check proxy: listening on {endpoint}, allowed hosts: {allowed}",
            flush=True,
        )
        async with server:
            await server.serve_forever()

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            # клиент закрыл соединение, не прислав запроса
            writer.close()
            return
        except asyncio.LimitOverrunError:
            await self._reply(
                writer,
                "431 Request Header Fields Too Large",
                "offline-check proxy: request head is over 64 KiB",
            )
            return

        try:
            head = RequestHead(raw)
        except OfflineCheckError as exc:
            await self._reply(writer, "400 Bad Request", str(exc))
            return

        if head.method == "CONNECT":
            await self._connect(head, reader, writer)
            return

        await self._forward(head, reader, writer)

    async def _connect(
        self,
        head: RequestHead,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parts = head.authority()
        host = parts.hostname
        if host is None:
            await self._reply(
                writer,
                "400 Bad Request",
                f"offline-check proxy: CONNECT expects host:port, got {head.target!r}",
            )
            return

        port = 443
        if parts.port is not None:
            port = parts.port

        upstream = await self._open(head.method, host, port, writer)
        if upstream is None:
            return

        writer.write(HttpReply.ESTABLISHED.encode("latin-1"))
        await self._tunnel.join((reader, writer), upstream)

    async def _forward(
        self,
        head: RequestHead,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parts = urlsplit(head.target)
        host = parts.hostname
        if parts.scheme != "http":
            await self._reply(
                writer,
                "400 Bad Request",
                "offline-check proxy: expected an absolute http:// target, "
                f"got {head.target!r}",
            )
            return

        if host is None:
            await self._reply(
                writer,
                "400 Bad Request",
                f"offline-check proxy: expected a host in {head.target!r}",
            )
            return

        port = 80
        if parts.port is not None:
            port = parts.port

        upstream = await self._open(head.method, host, port, writer)
        if upstream is None:
            return

        upstream[1].write(head.origin_form(parts))
        await self._tunnel.join((reader, writer), upstream)

    async def _open(
        self, method: str, host: str, port: int, writer: asyncio.StreamWriter
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        if not self._allowlist.allows(host):
            allowed = " ".join(self._allowlist.names())
            self._record(
                method, host, port, Verdict.DENIED, "host is not in the allowlist"
            )
            await self._reply(
                writer,
                "403 Forbidden",
                f"offline-check proxy: {host}:{port} is not a registry of the build "
                f"(allowed: {allowed})",
            )
            return None

        try:
            upstream = await asyncio.wait_for(
                asyncio.open_connection(host, port), self.CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as exc:
            self._record(
                method, host, port, Verdict.UPSTREAM_FAILED, f"connect failed: {exc!r}"
            )
            await self._reply(
                writer,
                "502 Bad Gateway",
                f"offline-check proxy: connecting to {host}:{port} failed: {exc!r}",
            )
            return None

        self._record(method, host, port, Verdict.ALLOWED, "")
        return upstream

    async def _reply(
        self, writer: asyncio.StreamWriter, status: str, text: str
    ) -> None:
        body = (text + "\n").encode("utf-8")
        head = HttpReply.STATUS.format(status=status, length=len(body))
        writer.write(head.encode("latin-1") + body)
        # клиент мог уйти, не дочитав отказ; решение уже в журнале
        with contextlib.suppress(ConnectionError):
            await writer.drain()

        writer.close()

    def _record(
        self, method: str, host: str, port: int, verdict: Verdict, detail: str
    ) -> None:
        event = ProxyEvent(time.time(), method, host, port, verdict, detail)
        self._journal.append(asdict(event))


class Relay:
    """Вход прокси в сети ловушки: TCP 127.0.0.1:<порт> → unix-сокет прокси.

    Шагам сборки прокси виден как http://127.0.0.1:<порт>; сетевых интерфейсов,
    кроме loopback, у них нет, поэтому прямое соединение наружу невозможно.
    """

    def __init__(self, endpoint: Path) -> None:
        self._endpoint = endpoint
        self._tunnel = Tunnel()

    async def serve(self, port: int) -> None:
        server = await asyncio.start_server(self._client, "127.0.0.1", port)
        print(
            f"offline-check jail: proxy relay 127.0.0.1:{port} -> {self._endpoint}",
            flush=True,
        )
        async with server:
            await server.serve_forever()

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream = await asyncio.open_unix_connection(str(self._endpoint))
        except OSError as exc:
            text = f"offline-check jail: proxy {self._endpoint} is unavailable: {exc}\n"
            print(text, file=sys.stderr, end="", flush=True)
            body = text.encode("utf-8")
            head = HttpReply.STATUS.format(status="502 Bad Gateway", length=len(body))
            writer.write(head.encode("latin-1") + body)
            writer.close()
            return

        await self._tunnel.join((reader, writer), upstream)


class DnsQuestion:
    """Вопрос DNS-запроса: имя и конец секции вопроса в пакете."""

    def __init__(self, packet: bytes) -> None:
        offset = int(DnsCode.HEADER_SIZE)
        labels: list[str] = []
        while True:
            if offset >= len(packet):
                raise OfflineCheckError(
                    f"dns: question name runs past the packet end ({len(packet)} bytes)"
                )

            size = packet[offset]
            if size == 0:
                break

            labels.append(
                packet[offset + 1 : offset + 1 + size].decode("ascii", "replace")
            )
            offset += size + 1

        self.name = ".".join(labels).lower()
        # ноль конца имени, тип и класс вопроса
        self.end = offset + 5


class DnsTrap:
    """Ловушка DNS сети шагов сборки: записывает имя и отвечает NXDOMAIN.

    Шаги сборки делят сеть контейнера-ловушки; его resolv.conf (docker run --dns)
    ведёт на 127.0.0.1, где слушает ловушка. Инструмент, который ходит через прокси,
    имён не разрешает, поэтому любое имя здесь — попытка соединиться напрямую. Суффикс
    search из resolv.conf отрезается: в журнал идёт имя, которое просил инструмент.
    """

    PACKET = 4096

    def __init__(self, journal: Journal, search: Sequence[str]) -> None:
        self._journal = journal
        self._search = tuple(search)

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.bind(("127.0.0.1", DnsCode.PORT))
            print(
                f"offline-check jail: dns trap on 127.0.0.1:{int(DnsCode.PORT)}",
                flush=True,
            )
            while True:
                data, addr = await loop.sock_recvfrom(sock, self.PACKET)
                await self._answer(loop, sock, data, addr)

    async def _answer(
        self,
        loop: asyncio.AbstractEventLoop,
        sock: socket.socket,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        try:
            question = DnsQuestion(data)
        except OfflineCheckError as exc:
            self._journal.append(asdict(DnsEvent(time.time(), f"<malformed: {exc}>")))
            return

        name = self._requested(question.name)
        self._journal.append(asdict(DnsEvent(time.time(), name)))
        await loop.sock_sendto(sock, self._nxdomain(data, question), addr)

    def _requested(self, name: str) -> str:
        for domain in self._search:
            suffix = f".{domain}"
            if name.endswith(suffix):
                return name.removesuffix(suffix)

        return name

    def _nxdomain(self, query: bytes, question: DnsQuestion) -> bytes:
        flags = int.from_bytes(query[2:4], "big") & DnsCode.FLAG_RECURSION_DESIRED
        flags |= (
            DnsCode.FLAG_RESPONSE
            | DnsCode.FLAG_RECURSION_AVAILABLE
            | DnsCode.RCODE_NXDOMAIN
        )
        counts = (1).to_bytes(2, "big") + bytes(6)
        return (
            query[0:2]
            + flags.to_bytes(2, "big")
            + counts
            + query[DnsCode.HEADER_SIZE : question.end]
        )


class ResolvConf:
    """Домены search из resolv.conf контейнера-ловушки."""

    def search(self, path: Path) -> list[str]:
        domains: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            words = line.split()
            if not words:
                continue

            if words[0] not in ("search", "domain"):
                continue

            domains.extend(words[1:])

        return domains


class BuildLine:
    """Строки классического сборщика: шаг «Step N/M : инструкция» и FROM … AS стадия."""

    STEP = re.compile(r"^Step (\d+)/\d+ : (.*)$")
    STAGE = re.compile(r"^FROM\s+\S+\s+AS\s+(\S+)", re.IGNORECASE)


class Stamp:
    """Фильтр вывода сборки: строки идут дальше как есть, цели и шаги — в отметки."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    def run(self) -> None:
        for raw in sys.stdin.buffer:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
            self._mark(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _mark(self, line: str) -> None:
        if line.startswith(MarkPrefix.TARGET):
            self._write(
                MarkKind.TARGET,
                line.removeprefix(MarkPrefix.TARGET).removesuffix(" ==").strip(),
            )
            return

        if line.startswith(MarkPrefix.HEADER):
            self._write(
                MarkKind.HEADER,
                line.removeprefix(MarkPrefix.HEADER).removesuffix(" ==").strip(),
            )
            return

        if BuildLine.STEP.match(line):
            self._write(MarkKind.STEP, line)

    def _write(self, kind: MarkKind, text: str) -> None:
        self._journal.append(asdict(Mark(time.time(), kind, text)))


class Timeline:
    """Место сборки на момент времени по отметкам stamp."""

    def __init__(self, marks: Sequence[Mark]) -> None:
        self._places = list(self._places_of(sorted(marks, key=self._time)))

    def at(self, moment: float) -> Place:
        place = Place("?", "", "", "")
        for start, current in self._places:
            if start > moment:
                break

            place = current

        return place

    def _time(self, mark: Mark) -> float:
        return mark.time

    def _places_of(self, marks: Iterable[Mark]) -> Iterator[tuple[float, Place]]:
        place = Place("?", "", "", "")
        for mark in marks:
            place = self._advance(place, mark)
            yield mark.time, place

    def _advance(self, place: Place, mark: Mark) -> Place:
        if mark.kind is MarkKind.TARGET:
            return Place(mark.text, "", "", "")

        if mark.kind is MarkKind.HEADER:
            return Place(place.target, mark.text, "", "")

        match = BuildLine.STEP.match(mark.text)
        if match is None:
            return place

        stage = place.stage
        if match.group(1) == "1":
            stage = ""

        if found := BuildLine.STAGE.match(match.group(2)):
            stage = found.group(1)

        return Place(place.target, place.header, stage, mark.text)


class Report:
    """Вердикт проверки: обращения к реестрам по хостам и нарушения с местом сборки."""

    def __init__(
        self,
        allowed: Sequence[AllowedHost],
        proxy: Sequence[ProxyEvent],
        dns: Sequence[DnsEvent],
        timeline: Timeline,
    ) -> None:
        self._allowed = allowed
        self._proxy = proxy
        self._dns = dns
        self._timeline = timeline

    def print(self) -> Exit:
        print("== offline-check: registries of the build ==")
        for line in self._registries():
            print(f"  {line}")

        print("== offline-check: requests through the proxy ==")
        for line in self._usage():
            print(f"  {line}")

        violations = list(dict.fromkeys(self._violations()))
        failures = list(dict.fromkeys(self._failures()))
        for line in failures:
            print(f"WARNING {line}")

        for line in violations:
            print(f"VIOLATION {line}")

        if violations:
            print(
                f"offline-check: FAILED, {len(violations)} access(es) outside the "
                "registries; fix each: move the download to make fetch or switch it off"
            )
            return Exit.VIOLATIONS

        print("offline-check: OK, the build reached only the registries above")
        return Exit.OK

    def _registries(self) -> Iterator[str]:
        sources: dict[tuple[str, Registry], list[str]] = {}
        for allowed in self._allowed:
            sources.setdefault((allowed.host, allowed.registry), []).append(
                allowed.source
            )

        for (host, registry), found in sources.items():
            joined = "; ".join(found)
            yield f"{registry:<4} {host:<32} from {joined}"

    def _usage(self) -> Iterator[str]:
        counts: dict[str, int] = {}
        for event in self._proxy:
            if event.verdict is not Verdict.ALLOWED:
                continue

            key = f"{event.host}:{event.port}"
            counts[key] = counts.get(key, 0) + 1

        for key in sorted(counts):
            yield f"{key:<46} {counts[key]} request(s)"

    def _violations(self) -> Iterator[str]:
        for event in self._proxy:
            if event.verdict is not Verdict.DENIED:
                continue

            place = self._timeline.at(event.time).render()
            yield (
                f"{place}: {event.method} {event.host}:{event.port} through the proxy, "
                "the host is not a registry of the build"
            )

        for event in self._dns:
            place = self._timeline.at(event.time).render()
            yield (
                f"{place}: direct lookup of {event.name} bypassing the proxy "
                "(a tool connects on its own, ignoring http_proxy/https_proxy)"
            )

    def _failures(self) -> Iterator[str]:
        for event in self._proxy:
            if event.verdict is not Verdict.UPSTREAM_FAILED:
                continue

            place = self._timeline.at(event.time).render()
            yield (
                f"{place}: registry {event.host}:{event.port} is allowed but "
                f"unreachable from the proxy: {event.detail}"
            )


class OfflineCheckCli:
    """Вход скрипта: разбор команды и вызов её исполнителя."""

    def __init__(self) -> None:
        self._parser = self._build_parser()
        self._indexes = IndexConfig()
        self._images = ImageConfig()
        self._reader = EventReader()
        self._resolv = ResolvConf()

    async def run(self, argv: Sequence[str]) -> Exit:
        args = self._parser.parse_args(argv)
        command = str(args.command)

        if command == "hosts":
            return self._hosts(args)

        if command == "proxy":
            allowlist = Allowlist(self._reader.allowed(Path(args.allow)))
            await ProxyServer(allowlist, Journal(Path(args.log))).serve(
                Path(args.socket)
            )
            return Exit.OK

        if command == "jail":
            return await self._jail(args)

        if command == "stamp":
            Stamp(Journal(Path(args.marks))).run()
            return Exit.OK

        timeline = Timeline(self._reader.marks(Path(args.marks)))
        allowed = self._reader.allowed(Path(args.allow))
        proxy = self._reader.proxy(Path(args.proxy))
        dns = self._reader.dns(Path(args.dns))
        return Report(allowed, proxy, dns, timeline).print()

    def _hosts(self, args: argparse.Namespace) -> Exit:
        hosts = list(self._indexes.hosts(Path(args.pip_conf), Path(args.uv_toml)))
        for image in args.apt_image:
            hosts.extend(self._images.apt(image))

        for image in args.npm_image:
            hosts.extend(self._images.npm(image))

        out = Journal(Path(args.out))
        for allowed in hosts:
            out.append(asdict(allowed))

        return Exit.OK

    async def _jail(self, args: argparse.Namespace) -> Exit:
        trap = DnsTrap(
            Journal(Path(args.log)), self._resolv.search(Path("/etc/resolv.conf"))
        )
        relay = Relay(Path(args.socket))
        await asyncio.gather(trap.serve(), relay.serve(int(args.port)))
        return Exit.OK

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="offline_check.py",
            description="Offline build check: registry allowlist, proxy, DNS trap",
        )
        commands = parser.add_subparsers(dest="command", required=True)

        hosts = commands.add_parser(
            "hosts", help="allowlist of registry hosts from the build configuration"
        )
        hosts.add_argument("--pip-conf", required=True)
        hosts.add_argument("--uv-toml", required=True)
        hosts.add_argument("--apt-image", action="append", default=[])
        hosts.add_argument("--npm-image", action="append", default=[])
        hosts.add_argument("--out", required=True)

        proxy = commands.add_parser(
            "proxy", help="HTTP proxy that lets through only the allowlist"
        )
        proxy.add_argument("--allow", required=True)
        proxy.add_argument("--log", required=True)
        proxy.add_argument("--socket", required=True)

        jail = commands.add_parser(
            "jail", help="network of the build steps: DNS trap and proxy relay"
        )
        jail.add_argument("--log", required=True)
        jail.add_argument("--socket", required=True)
        jail.add_argument("--port", required=True)

        stamp = commands.add_parser(
            "stamp", help="pass the build output through and mark targets and steps"
        )
        stamp.add_argument("--marks", required=True)

        report = commands.add_parser(
            "report", help="verdict from the proxy and DNS journals"
        )
        report.add_argument("--allow", required=True)
        report.add_argument("--proxy", required=True)
        report.add_argument("--dns", required=True)
        report.add_argument("--marks", required=True)
        return parser


async def main() -> int:
    try:
        return int(await OfflineCheckCli().run(sys.argv[1:]))
    except OfflineCheckError as exc:
        print(f"offline-check: {exc}", file=sys.stderr)
        return int(Exit.ERROR)


def cli() -> None:
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    cli()
