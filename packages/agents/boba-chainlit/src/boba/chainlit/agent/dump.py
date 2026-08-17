"""Диагностический httpx-транспорт: дамп HTTP-обмена с LLM-провайдером в файлы."""

import contextlib
import contextvars
import datetime
import logging
import typing
from collections.abc import Callable
from pathlib import Path

import httpcore
import httpx


class HttpDump:
    """Файл дампа одного запроса."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: typing.BinaryIO | None = None
        self._server: str | None = None
        self._client: str | None = None
        self._direction: int | None = None

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "ab")  # noqa: SIM115
        except Exception:
            self._fail("open")

    def _fail(self, op: str) -> None:
        logging.getLogger(type(self).__qualname__).exception(
            "request dump disabled: %s failed for %s", op, self.path
        )
        if self._file is not None:
            self._file.close()
        self._file = None

    @staticmethod
    def _format_addr(addr: typing.Any) -> str:
        match addr:
            case (host, port, *_):
                return f"{host}:{port}"
        return "?"

    @staticmethod
    def _get_now_partline():
        return datetime.datetime.now().strftime("%H:%M:%S.%f")

    @staticmethod
    def _get_header(direction: int, client: str, server: str):
        if direction == 1:
            tpl = "\n{now}: {server} <<< {client}:\n"
        else:
            tpl = "\n{now}: {client} >>> {server}:\n"

        return tpl.format_map(
            {
                "client": client,
                "server": server,
                "now": HttpDump._get_now_partline(),
            }
        )

    def _emit(self, data: bytes) -> None:
        if self._file is None:
            return

        try:
            self._file.write(data)
            self._file.flush()
        except Exception:
            self._fail("write")

    def emit_message(self, text: str) -> None:
        line = "\n{now}: {text}\n".format_map(
            {"now": self._get_now_partline(), "text": text},
        )
        self._emit(line.encode("utf-8", errors="ignore"))

    def write_bytes(
        self, inner: httpcore.AsyncNetworkStream, direction: int, data: bytes
    ) -> None:
        client = HttpDump._format_addr(inner.get_extra_info("client_addr"))
        server = HttpDump._format_addr(inner.get_extra_info("server_addr"))

        if not (
            client == self._client
            and server == self._server
            and self._direction == direction
        ):
            header = self._get_header(direction, client, server).encode(
                "utf-8",
                errors="ignore",
            )

            self._emit(header)
            self._client = client
            self._server = server
            self._direction = direction

        self._emit(data)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None

    def __enter__(self) -> "HttpDump":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class DumpChannel:
    """Канал передачи активного дампа от транспорта socket-уровню."""

    def __init__(self) -> None:
        self._current: contextvars.ContextVar[HttpDump | None] = contextvars.ContextVar(
            "http_dump", default=None
        )

    def get(self, inner: httpcore.AsyncNetworkStream) -> HttpDump | None:
        return self._current.get()

    @contextlib.contextmanager
    def activate(self, path: Path) -> typing.Iterator[HttpDump]:
        with HttpDump(path) as dump:
            prev = self._current.get()
            self._current.set(dump)
            try:
                yield dump
            finally:
                self._current.set(prev)


class DumpingTransport(httpx.AsyncHTTPTransport):
    """Открывает файл дампа с бизнес-именем; байты пишет socket-уровень."""

    def __init__(
        self,
        dump_dir: Path,
        dump_file: Callable[[httpx.Request], str],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._dump_dir = dump_dir
        self._request_label = dump_file
        self._channel = DumpChannel()
        self._pool._network_backend = LoggingBackend(
            self._pool._network_backend, self._channel
        )
        self.log = logging.getLogger(type(self).__qualname__)

    def _path(self, request: httpx.Request) -> Path:
        """Имя файла приходит из конфига: держим дамп внутри своего каталога."""
        base = self._dump_dir.resolve()
        path = (base / self._request_label(request)).resolve()

        if not path.is_relative_to(base):
            msg = f"dump file escapes {base}: {path}"
            raise ValueError(msg)

        return path

    async def handle_async_request(self, request: httpx.Request):
        with contextlib.ExitStack() as stack:
            _dump = stack.enter_context(self._channel.activate(self._path(request)))

            try:
                response = await super().handle_async_request(request)
            except Exception as exc:
                _dump.emit_message(f"attempt failed: {exc!r}")
                raise

            if not isinstance(response.stream, httpx.AsyncByteStream):
                raise TypeError(f"response.stream is not valid: {response.stream}")

            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                stream=DumpStream(response.stream, stack.pop_all(), self.log),
                extensions=response.extensions,
            )


class DumpStream(httpx.AsyncByteStream):
    def __init__(self, inner, stack: contextlib.ExitStack, log) -> None:
        self._inner = inner
        self._stack = stack
        self.log = log

    async def __aiter__(self):
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        self._stack.close()
        await self._inner.aclose()


class LoggingStream(httpcore.AsyncNetworkStream):
    """Пишет байты, реально прошедшие по сокету, в дамп текущего запроса."""

    def __init__(
        self, inner: httpcore.AsyncNetworkStream, channel: DumpChannel
    ) -> None:
        self._inner = inner
        self._channel = channel

    @staticmethod
    def _format_addr(addr: typing.Any) -> str:
        match addr:
            case (host, port, *_):
                return f"{host}:{port}"
        return "?"

    def _get_logrecord_data(self):
        client = self._format_addr(self._inner.get_extra_info("client_addr"))
        server = self._format_addr(self._inner.get_extra_info("server_addr"))

        return {
            "now": datetime.datetime.now().strftime("%H:%M:%S.%f"),
            "server": server,
            "client": client,
        }

    def direction(self, from_server: bool):
        if from_server == 1:
            tpl = "{now}: server {server} -> client {client}"
        else:
            tpl = "{now}: client {client} -> server {server}"

        return tpl.format_map(
            self._get_logrecord_data(),
        )

    async def read(self, max_bytes, timeout=None):
        data = await self._inner.read(max_bytes, timeout)

        if dump := self._channel.get(self._inner):
            dump.write_bytes(
                inner=self._inner,
                direction=1,
                data=data,
            )

        return data

    async def write(self, buffer, timeout=None):
        if dump := self._channel.get(self._inner):
            dump.write_bytes(
                inner=self._inner,
                direction=0,
                data=buffer,
            )

        await self._inner.write(buffer, timeout)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return LoggingStream(
            await self._inner.start_tls(ssl_context, server_hostname, timeout),
            self._channel,
        )

    async def aclose(self):
        await self._inner.aclose()

    def get_extra_info(self, info):
        return self._inner.get_extra_info(info)


class LoggingBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self, inner: httpcore.AsyncNetworkBackend, channel: DumpChannel
    ) -> None:
        self._inner = inner
        self._channel = channel

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return LoggingStream(
            await self._inner.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            ),
            self._channel,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return LoggingStream(
            await self._inner.connect_unix_socket(
                path,
                timeout=timeout,
                socket_options=socket_options,
            ),
            self._channel,
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)
