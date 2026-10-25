"""Проверка собранного образа приложения boba (make test): интерпретатор, пакеты boba-*,
консольные точки входа; у приложения с песочницей — образы корней плагинов и модели
рядом с релизом, у чата — файлы ассетов, названные конфигом chainlit; у чата и
службы агента — самопроверка агента boba (`boba self-check`: данные пакетов внутри
бинарника) и его ход с моделью проверки.

Ход идёт с историей в памяти: в контейнере проверки нет сети, и postgres, где лежит
история агента в проде, недоступен. Файлы схемы истории postgres внутри бинарника
проверяет самопроверка; подключение, запись и возобновление сессии в postgres —
тест бинарника test_cli_binary.py (маркер binary) на базе стенда.

Скрипт идёт на stdin интерпретатора образа с окружением conf/boba.env, без сети:
  python3 - --app aid|mcp|agent --base /app/boba --names <app/packages.txt> \
    [--sandbox --ocr <модель>... --model-images <модель>...]
Последняя строка вывода — RESULT: ALL OK либо RESULT: FAIL со списком проваленных
проверок.
"""

from __future__ import annotations

import argparse
import functools
import glob
import importlib
import importlib.metadata
import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit


class ImageCheckError(Exception):
    """Отказ одной проверки образа: текст уходит в строку FAIL отчёта."""


class App(StrEnum):
    """Приложение образа: от него зависят сторонние пакеты и проверка агента."""

    AID = "aid"
    MCP = "mcp"
    BOBA = "boba"

    @property
    def third_party(self) -> tuple[str, ...]:
        if self is App.AID:
            return ("pydantic", "fastapi", "chainlit")
        if self is App.BOBA:
            return ("pydantic", "fastmcp", "acp")
        return ("pydantic", "fastmcp")

    @property
    def runs_agent(self) -> bool:
        """В образе приложения есть агент boba: служба агента и есть он сам
        (`boba serve`); чат ходит в неё по сети, своего агента у него нет."""
        return self is App.BOBA


class SandboxOnly(StrEnum):
    """Модули разбора и эмбеддинга, которым место в песочнице, а не у приложения."""

    PYPDFIUM2 = "pypdfium2"
    RAPIDOCR = "rapidocr"
    MARKDOWNIFY = "markdownify"
    BS4 = "bs4"
    FASTEMBED = "fastembed"
    ONNXRUNTIME = "onnxruntime"
    PANDAS = "pandas"
    OPENPYXL = "openpyxl"


class ChainlitAsset(StrEnum):
    """Поля [UI] .chainlit/config.toml, которые называют файл app_root: без файла
    страница получает 404 вместо стиля или аватара."""

    CUSTOM_CSS = "custom_css"
    AVATAR = "default_avatar_file_url"


class BareEnv(StrEnum):
    """Переменные окружения, которые SDK ACP оставляет процессу агента."""

    HOME = "HOME"
    LOGNAME = "LOGNAME"
    PATH = "PATH"
    SHELL = "SHELL"
    TERM = "TERM"
    USER = "USER"


@dataclass(frozen=True)
class Options:
    """Разобранный вызов проверки: приложение, корень релиза в образе, список пакетов
    boba из образа, файлы моделей OCR, модели с образами данных песочницы и команда
    агента (только у службы агента)."""

    app: App
    base: Path
    names: Sequence[str]
    sandbox: bool
    ocr: Sequence[str]
    model_images: Sequence[str]
    agent: str


class OptionsParser:
    """Разбор аргументов проверки в Options: список пакетов читается здесь же, агент
    службы — исполняемый AGENT_BIN под корнем релиза, его же запускает образ."""

    AGENT_BIN: ClassVar[str] = "bin/boba.dist/boba.bin"

    def __init__(self) -> None:
        self._parser = argparse.ArgumentParser(prog="bobacheck")
        self._parser.add_argument("--app", required=True, choices=list(App))
        self._parser.add_argument("--base", required=True, type=Path)
        self._parser.add_argument("--names", required=True, type=Path)
        self._parser.add_argument("--sandbox", action="store_true")
        self._parser.add_argument("--ocr", nargs="+", default=[])
        self._parser.add_argument("--model-images", nargs="+", default=[])

    def parse(self, argv: Sequence[str]) -> Options:
        args = self._parser.parse_args(argv)
        app = App(args.app)
        names_file: Path = args.names

        if not names_file.is_file():
            self._parser.error(
                f"нет списка пакетов {names_file}: его пишет стадия app-site"
            )

        agent = ""
        if app.runs_agent:
            agent = str(args.base / self.AGENT_BIN)

        return Options(
            app=app,
            base=args.base,
            names=self._names(names_file),
            sandbox=args.sandbox,
            ocr=tuple(args.ocr),
            model_images=tuple(args.model_images),
            agent=agent,
        )

    def _names(self, path: Path) -> tuple[str, ...]:
        lines = path.read_text(encoding="utf-8").splitlines()
        names = sorted(self._stripped(lines))
        return tuple(names)

    def _stripped(self, lines: Sequence[str]) -> Iterator[str]:
        for line in lines:
            name = line.strip()
            if not name:
                continue
            yield name


class Report:
    """Отчёт проверки: строка на проверку; проваленные копятся до строки RESULT."""

    def __init__(self) -> None:
        self._failures: list[str] = []

    def section(self, title: str) -> None:
        print(f"== {title} ==", flush=True)

    def note(self, label: str, value: object) -> None:
        print(f"  {label:<11}: {value}", flush=True)

    def check(self, name: str, probe: Callable[[], str]) -> None:
        # граница проверки: любой отказ становится строкой FAIL с причиной, а не обрывом
        try:
            detail = probe()
        except Exception as exc:
            print(f"  FAIL {name}: {exc}", flush=True)
            self._failures.append(name)
            return

        print(f"  OK   {name}{self._suffix(detail)}", flush=True)

    def warn(self, name: str, probe: Callable[[], str]) -> None:
        try:
            detail = probe()
        except Exception as exc:
            print(f"  WARN {name}: {exc}", flush=True)
            return

        print(f"  OK   {name}{self._suffix(detail)}", flush=True)

    def result(self) -> int:
        print()
        if self._failures:
            print(f"RESULT: FAIL ({len(self._failures)}): {', '.join(self._failures)}")
            return 1

        print("RESULT: ALL OK")
        return 0

    def _suffix(self, detail: str) -> str:
        if not detail:
            return ""
        return f" — {detail}"


class CheckModel(BaseHTTPRequestHandler):
    """Модель проверки: на любой POST отвечает OpenAI-совместимым потоком SSE из одной
    реплики ANSWER и конца хода. Её поднимает AgentTurn на 127.0.0.1 в своём процессе:
    сети у контейнера проверки нет."""

    ANSWER: ClassVar[str] = "pong from the check model"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in self._chunks():
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — сигнатура http.server
        return

    def _chunks(self) -> Iterator[Mapping[str, object]]:
        delta = {"role": "assistant", "content": self.ANSWER}
        yield {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        end = {"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}
        yield {"choices": [end], "usage": usage}


@dataclass(frozen=True)
class TurnOutcome:
    """Итог хода агента: кто ответил (имя и версия из initialize), что сказал и чем
    кончил ход (stopReason последнего state_update idle)."""

    agent: str
    answer: str
    stop_reason: str


class AgentTurn:
    """Агент чата так, как его запускает чат: команда из BOBA_AGENT_COMMAND в
    окружении BareEnv. self_check() зовёт `self-check` бинарника; run() ведёт ход —
    ACP v2 по stdio: initialize, session/new, session/prompt и поток session/update до
    state_update idle. Модель — CheckModel в потоке этого процесса; уведомления,
    пришедшие раньше ответа на запрос, ждут разбора в очереди."""

    PROTOCOL: ClassVar[int] = 2
    DEADLINE_SEC: ClassVar[float] = 120.0
    SELF_CHECK_SEC: ClassVar[float] = 60.0
    LOG_TAIL: ClassVar[int] = 1500

    def __init__(self, command: str) -> None:
        self._command = command
        self._ids = itertools.count(1)
        self._backlog: list[Mapping[str, Any]] = []

    def self_check(self) -> str:
        argv = [self._command, "self-check"]
        try:
            done = subprocess.run(
                argv,
                env=self._bare_env(),
                capture_output=True,
                text=True,
                timeout=self.SELF_CHECK_SEC,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ImageCheckError(
                f"{self._command} self-check did not finish in "
                f"{self.SELF_CHECK_SEC:.0f} s"
            ) from exc
        except OSError as exc:
            raise ImageCheckError(
                f"{self._command} self-check did not start: {exc}"
            ) from exc

        if done.returncode != 0:
            tail = done.stderr[-self.LOG_TAIL :]
            raise ImageCheckError(
                f"{self._command} self-check exited with {done.returncode}, expected "
                f"0: {tail}"
            )

        return done.stdout.strip()

    def run(self, workdir: Path) -> TurnOutcome:
        server = ThreadingHTTPServer(("127.0.0.1", 0), CheckModel)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            settings = self._settings(workdir, server.server_port)
            return self._talk(workdir, settings)
        finally:
            server.shutdown()
            server.server_close()

    def _settings(self, workdir: Path, port: int) -> Path:
        provider = {
            "kind": "openai",
            "baseUrl": f"http://127.0.0.1:{port}/v1",
            "apiKey": "check",
        }
        model = {
            "provider": provider,
            "model": "check-model",
            "contextWindow": 32000,
            "maxOutputTokens": 512,
        }
        document = {
            "model": "check",
            "models": {"check": model},
            "history": {"kind": "memory"},
            "limits": {"toolResultWindowShare": 0.25},
            "permissions": {"defaultMode": "bypassPermissions", "allowBypass": True},
        }
        path = workdir / "settings.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

        return path

    def _talk(self, workdir: Path, settings: Path) -> TurnOutcome:
        log_path = workdir / "agent.log"
        argv = [
            self._command,
            "--stdio",
            "--setting-sources",
            "",
            "--settings",
            str(settings),
        ]
        with (
            log_path.open("w", encoding="utf-8") as log,
            subprocess.Popen(
                argv,
                env=self._bare_env(),
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
            ) as agent,
        ):
            watchdog = threading.Timer(self.DEADLINE_SEC, agent.kill)
            watchdog.start()
            try:
                return self._converse(agent, workdir)
            except ImageCheckError as exc:
                agent.kill()
                agent.wait()
                tail = log_path.read_text(encoding="utf-8")[-self.LOG_TAIL :]
                raise ImageCheckError(f"{exc}; журнал агента: {tail}") from exc
            finally:
                watchdog.cancel()

    def _bare_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in BareEnv:
            if name in os.environ:
                env[name] = os.environ[name]

        return env

    def _converse(self, agent: subprocess.Popen[str], workdir: Path) -> TurnOutcome:
        info = {"name": "bobacheck", "version": "0"}
        init_params = {
            "protocolVersion": self.PROTOCOL,
            "info": info,
            "capabilities": {},
        }
        init = self._call(agent, "initialize", init_params)
        if init["protocolVersion"] != self.PROTOCOL:
            raise ImageCheckError(f"{self._command}: initialize ответил {init}")

        name = f"{init['info']['name']} {init['info']['version']}"
        opened = self._call(
            agent, "session/new", {"cwd": str(workdir), "mcpServers": []}
        )
        session = opened["sessionId"]
        prompt = [{"type": "text", "text": "ping"}]
        self._call(agent, "session/prompt", {"sessionId": session, "prompt": prompt})

        texts: list[str] = []
        for update in self._updates(agent):
            texts.extend(self._texts(update))
            if update.get("sessionUpdate") != "state_update":
                continue
            if update.get("state") != "idle":
                continue
            return TurnOutcome(
                agent=name, answer="".join(texts), stop_reason=update["stopReason"]
            )

        raise ImageCheckError(f"{self._command}: ход кончился без state_update idle")

    def _call(
        self, agent: subprocess.Popen[str], method: str, params: Mapping[str, object]
    ) -> Mapping[str, Any]:
        ident = next(self._ids)
        request = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        self._send(agent, request)
        while True:
            message = self._read(agent)
            if "method" in message:
                self._backlog.append(message)
                continue
            if message.get("id") != ident:
                continue
            if "error" in message:
                raise ImageCheckError(
                    f"{method}: агент ответил ошибкой {message['error']}"
                )
            return message["result"]

    def _updates(self, agent: subprocess.Popen[str]) -> Iterator[Mapping[str, Any]]:
        while True:
            message = self._next(agent)
            if message.get("method") != "session/update":
                continue
            yield message["params"]["update"]

    def _next(self, agent: subprocess.Popen[str]) -> Mapping[str, Any]:
        if self._backlog:
            return self._backlog.pop(0)
        return self._read(agent)

    def _texts(self, update: Mapping[str, Any]) -> Iterator[str]:
        if update.get("sessionUpdate") not in ("agent_message_chunk", "agent_message"):
            return
        content = update.get("content")
        if isinstance(content, dict):
            yield str(content.get("text", ""))
        if isinstance(content, list):
            for block in content:
                yield str(block.get("text", ""))

    def _send(
        self, agent: subprocess.Popen[str], message: Mapping[str, object]
    ) -> None:
        stdin = agent.stdin
        if stdin is None:
            raise ImageCheckError(f"{self._command}: процесс агента без stdin")
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()

    def _read(self, agent: subprocess.Popen[str]) -> Mapping[str, Any]:
        stdout = agent.stdout
        if stdout is None:
            raise ImageCheckError(f"{self._command}: процесс агента без stdout")
        line = stdout.readline()
        if not line:
            raise ImageCheckError(
                f"{self._command}: агент закрыл вывод, код {agent.poll()}"
            )
        return json.loads(line)


class ImageCheck:
    """Проверка образа приложения по разделам: каждый пишет строки в Report, итог —
    код выхода make test. Ход агента ведёт AgentTurn — у чата и службы агента."""

    TOOLS_GROUP: ClassVar[str] = "boba.tools"
    END_TURN: ClassVar[str] = "end_turn"

    def __init__(self, options: Options) -> None:
        self._options = options
        self._report = Report()
        self._turn = AgentTurn(options.agent)
        self._plugins_dir = options.base / "sandbox" / "plugins"
        self._models_dir = options.base / "models"
        self._app_root = options.base / "app_root"

    def run(self) -> int:
        self._interpreter()
        self._third_party()
        self._distributions()
        self._console_scripts()
        self._parsers()
        if self._options.sandbox:
            self._sandbox()
        if self._options.app is App.AID:
            self._assets()
        if self._options.app.runs_agent:
            self._agent()
        return self._report.result()

    def _interpreter(self) -> None:
        self._report.section("интерпретатор")
        self._report.note("python", sys.version.split()[0])
        self._report.note("executable", sys.executable)

    def _third_party(self) -> None:
        self._report.section("сторонние пакеты")
        for module in self._options.app.third_party:
            self._report.check(
                f"import {module}", functools.partial(self._import, module)
            )

    def _distributions(self) -> None:
        self._report.section("пакеты boba")
        self._report.check(f"список {len(self._options.names)} пакетов", self._listed)
        for name in self._options.names:
            self._report.check(
                f"пакет {name}", functools.partial(importlib.metadata.version, name)
            )

    def _console_scripts(self) -> None:
        self._report.section("консольные точки входа")
        scripts = sorted(set(self._script_names()))
        self._report.note("объявлено", len(scripts))
        for script in scripts:
            self._report.check(
                f"which {script}", functools.partial(self._on_path, script)
            )

    def _parsers(self) -> None:
        # у mcp их тянут пакеты плагинов: предупреждение, а не отказ
        self._report.section("парсеры остаются в песочнице")
        for module in SandboxOnly:
            self._report.warn(
                f"нет {module} у приложения", functools.partial(self._absent, module)
            )

    def _sandbox(self) -> None:
        self._report.section(
            "песочница: образы корней плагинов и модели рядом с релизом"
        )
        self._report.check("образы корней плагинов", self._plugin_images)
        self._report.check("веса эмбеддера fastembed", self._embed_weights)
        self._report.check("модели OCR", self._ocr_models)
        self._report.check("образы данных моделей", self._model_images)

    def _assets(self) -> None:
        self._report.section("ассеты chainlit, названные в .chainlit/config.toml")
        for asset in ChainlitAsset:
            self._report.check(
                f"[UI] {asset.value}", functools.partial(self._asset_file, asset)
            )

    def _agent(self) -> None:
        self._report.section("агент чата в окружении SDK ACP")
        self._report.check(f"самопроверка {self._options.agent}", self._turn.self_check)
        self._report.check(
            f"ход агента {self._options.agent} (история в памяти)", self._agent_turn
        )

    def _import(self, module: str) -> str:
        importlib.import_module(module)
        return ""

    def _listed(self) -> str:
        if not self._options.names:
            raise ImageCheckError(
                "список пакетов пуст: стадия app-site не нашла пакетов boba"
            )
        return ""

    def _script_names(self) -> Iterator[str]:
        for dist in importlib.metadata.distributions():
            if not dist.metadata["Name"].lower().startswith("boba-"):
                continue
            for entry in dist.entry_points:
                if entry.group != "console_scripts":
                    continue
                yield entry.name

    def _on_path(self, script: str) -> str:
        found = shutil.which(script)
        if found is None:
            raise ImageCheckError(f"{script} нет в PATH")
        return found

    def _absent(self, module: str) -> str:
        if importlib.util.find_spec(module) is not None:
            raise ImageCheckError(f"{module} в окружении приложения")
        return ""

    def _plugin_names(self) -> Iterator[str]:
        """Плагины — имена entry points группы boba.tools: у каждого свой образ."""
        for entry in importlib.metadata.entry_points(group=self.TOOLS_GROUP):
            yield entry.name

    def _missing_images(self, names: Sequence[str]) -> Iterator[str]:
        for name in names:
            if (self._plugins_dir / f"{name}.ext4").is_file():
                continue
            yield name

    def _plugin_images(self) -> str:
        names = sorted(set(self._plugin_names()))
        if not names:
            raise ImageCheckError(
                f"у приложения нет пакетов с entry points {self.TOOLS_GROUP}"
            )

        missing = list(self._missing_images(names))
        if missing:
            raise ImageCheckError(
                f"нет образов <id>.ext4 в {self._plugins_dir}: {', '.join(missing)}"
            )

        return f"образов: {len(names)}"

    def _embed_weights(self) -> str:
        root = self._models_dir / "fastembed"
        pattern = str(root / "models--*" / "**" / "*.onnx")
        weights = glob.glob(pattern, recursive=True)
        if not weights:
            raise ImageCheckError(f"нет весов *.onnx в {root}")
        return f"файлов весов: {len(weights)}"

    def _ocr_models(self) -> str:
        root = self._models_dir / "rapidocr"
        if not self._options.ocr:
            raise ImageCheckError(
                "модели OCR не названы: make test передаёт их аргументом --ocr"
            )

        for name in self._options.ocr:
            if not (root / name).is_file():
                raise ImageCheckError(f"нет модели {name} в {root}")

        return f"моделей: {len(self._options.ocr)}"

    EXT4_MAGIC_AT: ClassVar[int] = 1080
    EXT4_MAGIC: ClassVar[bytes] = b"\x53\xef"

    def _model_images(self) -> str:
        """Образы весов рядом с каталогами моделей: песочница монтирует их вместо
        каталогов, и без файла секция не поднимется."""
        if not self._options.model_images:
            raise ImageCheckError(
                "образы данных не названы: make test передаёт их аргументом "
                "--model-images"
            )

        for name in self._options.model_images:
            image = self._models_dir / f"{name}.ext4"
            if not image.is_file():
                raise ImageCheckError(f"нет образа данных {image}: make models-image")

            with image.open("rb") as handle:
                handle.seek(self.EXT4_MAGIC_AT)
                magic = handle.read(len(self.EXT4_MAGIC))

            if magic != self.EXT4_MAGIC:
                raise ImageCheckError(
                    f"{image}: expected an ext4 superblock magic 0xEF53 at byte "
                    f"{self.EXT4_MAGIC_AT}, got {magic.hex()}"
                )

        return f"образов: {len(self._options.model_images)}"

    def _asset_file(self, asset: ChainlitAsset) -> str:
        config = self._app_root / ".chainlit" / "config.toml"
        with config.open("rb") as handle:
            settings = tomllib.load(handle)

        ui = settings.get("UI")
        if not isinstance(ui, dict):
            raise ImageCheckError(
                f"{config}: expected the [UI] section of chainlit, got {ui!r}"
            )

        address = str(ui.get(asset.value, ""))
        if not address:
            return "not set"

        parts = urlsplit(address)
        if parts.netloc:
            return f"external {address}"

        path = self._app_root / parts.path.lstrip("/")
        if not path.is_file():
            raise ImageCheckError(
                f"{config}: [UI] {asset.value} = {address!r} expects the file {path} "
                "in the image, it is missing"
            )

        return str(path)

    def _agent_turn(self) -> str:
        with tempfile.TemporaryDirectory() as workdir:
            outcome = self._turn.run(Path(workdir))

        if CheckModel.ANSWER not in outcome.answer:
            raise ImageCheckError(
                f"{outcome.agent}: в ответе нет реплики модели: {outcome.answer!r}"
            )
        if outcome.stop_reason != self.END_TURN:
            raise ImageCheckError(
                f"{outcome.agent}: ход кончился {outcome.stop_reason}, "
                f"а не {self.END_TURN}"
            )

        return f"{outcome.agent} ответил {outcome.answer!r}"


if __name__ == "__main__":
    # chainlit на импорте создаёт .chainlit/.files в cwd: уводим в /tmp
    os.environ["CHAINLIT_APP_ROOT"] = "/tmp"
    sys.exit(ImageCheck(OptionsParser().parse(sys.argv[1:])).run())
