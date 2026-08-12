"""Канальный контракт pg-payload'а: запрос из tool_args, отказ конвертом.

Payload запускается настоящим процессом на реальных pipe-каналах. Отказы
проверяются на заведомо недоступной базе; двунаправленный COPY — на живом
стенде, адрес которого приходит переменной окружения PgStand.DSN_ENV.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict

from boba.toolkit.channels import Channel
from boba.toolkit.payload import PayloadExit

PASSWORD = "s3cret"

DEAD_CONNECTION: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 1,
    "dbname": "db",
    "user": "u",
    "password": PASSWORD,
    "connect_timeout": 1,
}

ENTRY: tuple[str, ...] = ("-m", "boba.tool.pg.payload")


class PayloadRun(BaseModel):
    """Итог прогона payload'а: код возврата, конверт, данные и stderr."""

    model_config = ConfigDict(frozen=True)

    code: int
    envelope: dict[str, Any]
    payload: bytes
    stderr: str

    def error(self) -> dict[str, Any]:
        body = self.envelope["error"]
        assert isinstance(body, dict)

        return body

    def data(self) -> dict[str, Any]:
        body = self.envelope["data"]
        assert isinstance(body, dict)

        return body


def _read_all(fd: int) -> bytes:
    data = bytearray()
    while True:
        piece = os.read(fd, 65536)
        if not piece:
            break
        data.extend(piece)

    os.close(fd)

    return bytes(data)


def _run(request: Mapping[str, Any]) -> PayloadRun:
    """Прогон без входного канала: узел графа без ребра и литерала."""
    return _execute(request, feed=b"", fed=False)


def _run_fed(request: Mapping[str, Any], feed: bytes) -> PayloadRun:
    """Прогон с входным каналом: байты уезжают в tool_stdin и канал закрывается."""
    return _execute(request, feed=feed, fed=True)


def _execute(request: Mapping[str, Any], *, feed: bytes, fed: bool) -> PayloadRun:
    args_r, args_w = os.pipe()
    result_r, result_w = os.pipe()
    payload_r, payload_w = os.pipe()

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    env[Channel.TOOL_ARGS.env_name] = str(args_r)
    env[Channel.TOOL_RESULT.env_name] = str(result_w)
    env[Channel.TOOL_PAYLOAD.env_name] = str(payload_w)
    env[Channel.TOOL_STDOUT.env_name] = "1"
    env[Channel.TOOL_STDERR.env_name] = "2"

    passed: list[int] = [args_r, result_w, payload_w]

    stdin_w = -1
    if fed:
        stdin_r, stdin_w = os.pipe()
        env[Channel.TOOL_STDIN.env_name] = str(stdin_r)
        passed.append(stdin_r)

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, *ENTRY],
        pass_fds=tuple(passed),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    for fd in passed:
        os.close(fd)

    os.write(args_w, json.dumps(request, ensure_ascii=False).encode("utf-8"))
    os.close(args_w)

    if fed:
        _feed_stdin(stdin_w, feed)

    _, stderr = proc.communicate(timeout=60)

    raw = _read_all(result_r).decode("utf-8").strip()
    payload = _read_all(payload_r)

    return PayloadRun(
        code=proc.returncode,
        envelope=json.loads(raw),
        payload=payload,
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _feed_stdin(fd: int, feed: bytes) -> None:
    """Вход стадии; ушедший payload делает запись EPIPE — это его отказ, не тест."""
    try:  # noqa: SIM105
        os.write(fd, feed)
    except BrokenPipeError:
        # payload ушёл раньше записи: его отказ проверяет конверт, не запись
        pass

    os.close(fd)


class TestPgPayloadChannels:
    def test_unreachable_database_is_a_declared_failure(self) -> None:
        run = _run(
            {
                "op": "pg_query",
                "connection": DEAD_CONNECTION,
                "sql": "select 1",
                "params": [],
                "row_limit": 10,
            }
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "database_unavailable"
        assert run.payload == b""

    def test_broken_request_names_the_field_without_echoing_it(self) -> None:
        run = _run(
            {
                "op": "pg_copy",
                "connection": DEAD_CONNECTION,
                "direction": "sideways",
                "sql": "COPY people FROM STDIN WITH (FORMAT CSV)",
            }
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "invalid_request"
        assert "direction" in run.error()["message"]
        assert "sideways" not in run.error()["message"]

    def test_credentials_never_leak_into_the_report(self) -> None:
        run = _run(
            {
                "op": "pg_query",
                "connection": DEAD_CONNECTION,
                "sql": "select 1",
                "params": [],
                "row_limit": 0,
            }
        )

        assert run.error()["kind"] == "invalid_request"
        assert PASSWORD not in json.dumps(run.envelope, ensure_ascii=False)
        assert PASSWORD not in run.stderr


class TestPgCopyIn:
    """Заливка COPY ... FROM STDIN: отказы без базы, вход только байтами."""

    LOAD: str = "COPY people (id, name) FROM STDIN WITH (FORMAT CSV)"

    def test_copy_from_stdin_without_input_is_a_declared_failure(self) -> None:
        run = _run(
            {
                "op": "pg_copy",
                "connection": DEAD_CONNECTION,
                "direction": "from_stdin",
                "sql": self.LOAD,
            }
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "no_input"
        assert run.payload == b""

    def test_copy_into_unreachable_database_is_a_declared_failure(self) -> None:
        run = _run_fed(
            {
                "op": "pg_copy",
                "connection": DEAD_CONNECTION,
                "direction": "from_stdin",
                "sql": self.LOAD,
            },
            b"1,Ivan\n",
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "database_unavailable"
        assert run.payload == b""


class PgStand:
    """Живой postgres для проверки COPY; адрес приходит переменной окружения."""

    DSN_ENV: ClassVar[str] = "BOBA_TEST_PG_DSN"

    TABLE: ClassVar[str] = "boba_copy_stand"

    KEYS: ClassVar[tuple[str, ...]] = ("host", "port", "dbname", "user", "password")

    @classmethod
    def dsn(cls) -> str:
        """DSN стенда; без переменной окружения тест пропускается."""
        dsn = os.environ.get(cls.DSN_ENV)
        if not dsn:
            pytest.skip(f"{cls.DSN_ENV} is not set: live postgres stand is absent")

        return dsn

    @classmethod
    def connection(cls) -> dict[str, Any]:
        """Профиль подключения стенда для tool_args payload'а."""
        parsed = conninfo_to_dict(cls.dsn())

        profile: dict[str, Any] = {"connect_timeout": 5}
        for key in cls.KEYS:
            value = parsed.get(key)
            if value is None:
                continue

            profile[key] = value

        return profile

    @classmethod
    def load_sql(cls) -> str:
        return f"COPY {cls.TABLE} (id, name) FROM STDIN WITH (FORMAT CSV)"

    @classmethod
    def dump_sql(cls) -> str:
        query = f"SELECT id, name FROM {cls.TABLE} ORDER BY id"  # noqa: S608

        return f"COPY ({query}) TO STDOUT WITH (FORMAT CSV)"

    @classmethod
    def reset(cls) -> None:
        """Пустая таблица стенда: имя одно, прогоны не наследуют строки."""
        drop = sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(cls.TABLE))
        create = sql.SQL("CREATE TABLE {} (id int, name text)").format(
            sql.Identifier(cls.TABLE)
        )

        with psycopg.connect(cls.dsn()) as conn:
            conn.execute(drop)
            conn.execute(create)

    @classmethod
    def rows(cls) -> Sequence[tuple[Any, ...]]:
        select = sql.SQL("SELECT id, name FROM {} ORDER BY id").format(
            sql.Identifier(cls.TABLE)
        )

        with psycopg.connect(cls.dsn()) as conn:
            cursor = conn.execute(select)

            return cursor.fetchall()


class TestPgCopyStand:
    """Двунаправленный COPY на живой базе: один узел заливает и выгружает."""

    def test_copy_from_stdin_loads_the_input(self) -> None:
        connection = PgStand.connection()
        PgStand.reset()

        run = _run_fed(
            {
                "op": "pg_copy",
                "connection": connection,
                "direction": "from_stdin",
                "sql": PgStand.load_sql(),
            },
            "1,Ivan\n2,Анна\n".encode(),
        )

        assert run.code == PayloadExit.OK
        assert run.data() == {"direction": "from_stdin", "rows": 2}
        assert run.payload == b""
        assert list(PgStand.rows()) == [(1, "Ivan"), (2, "Анна")]

    def test_broken_input_leaves_the_table_empty(self) -> None:
        """Заливка транзакционна: битая строка посреди потока не оставляет следа."""
        connection = PgStand.connection()
        PgStand.reset()

        run = _run_fed(
            {
                "op": "pg_copy",
                "connection": connection,
                "direction": "from_stdin",
                "sql": PgStand.load_sql(),
            },
            b"1,Ivan\n2,Anna\nnot-a-number,Boris\n",
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "sql_failed"
        assert list(PgStand.rows()) == []

    def test_copy_to_stdout_streams_the_loaded_rows(self) -> None:
        connection = PgStand.connection()
        PgStand.reset()

        _run_fed(
            {
                "op": "pg_copy",
                "connection": connection,
                "direction": "from_stdin",
                "sql": PgStand.load_sql(),
            },
            b"7,Sergey\n",
        )

        run = _run(
            {
                "op": "pg_copy",
                "connection": connection,
                "direction": "to_stdout",
                "sql": PgStand.dump_sql(),
            }
        )

        assert run.code == PayloadExit.OK
        assert run.data() == {"direction": "to_stdout", "rows": 1}
        assert run.payload == b"7,Sergey\n"

    def test_copy_of_a_broken_row_is_a_declared_failure(self) -> None:
        connection = PgStand.connection()
        PgStand.reset()

        run = _run_fed(
            {
                "op": "pg_copy",
                "connection": connection,
                "direction": "from_stdin",
                "sql": PgStand.load_sql(),
            },
            b"not-a-number,Ivan\n",
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "sql_failed"
        assert list(PgStand.rows()) == []
