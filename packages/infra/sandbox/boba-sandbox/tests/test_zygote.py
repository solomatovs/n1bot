"""Зигота: жизненный цикл супервизора, протокол вызова, изоляция детей.

Юнит-сценарии гоняют зиготу голым python'ом (isolate=False): рестарты,
исчерпание попыток, отказ вызова при мёртвой зиготе. E2e — в настоящем bwrap
с CAP_SYS_ADMIN: изоляция /tmp и pid, сброс capabilities.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fake_channel_tool import ChannelConfig
from pydantic import SecretStr
from zygote_stand import SandboxStand

from boba.cancellation import ToolStopped, run_cancellation
from boba.sandbox.guest import CallMounts, ChildLimits, WarmupCall, ZygoteArgs
from boba.sandbox.zygote import (
    ZygoteCallError,
    ZygotePolicy,
    ZygoteState,
    ZygoteSupervisor,
    ZygoteUnavailableError,
)
from boba.toolkit.channels import ToolChannel
from boba.toolkit.protocol import REPLY, ReplyOk
from boba.toolkit.stream import Chunk

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "build" / "chainlit" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"

TESTS_DIR = str(Path(__file__).resolve().parent)

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make fetch sandbox)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)

CFG = ChannelConfig(token=SecretStr("zy-s3cret"))

FAST = ZygotePolicy(
    start_timeout_sec=10.0,
    max_start_attempts=3,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)

NO_LIMITS = ChildLimits()


class Recorder:
    """Приёмник канала: копит байты."""

    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, chunk: Chunk) -> None:
        self.data.extend(chunk)

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


WARMUP_CALLS = (
    WarmupCall(
        module="fake_channel_tool", hook="warm_cache", config={"greeting": "privet"}
    ),
)
"""Конфиг прогрева фейкового модуля: без него зигота не поднимается."""


def _plain_spawner(fd: int) -> subprocess.Popen[bytes]:
    """Зигота голым python'ом процесса тестов: без bwrap, без изоляции."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([TESTS_DIR, env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )

    args = ZygoteArgs(
        socket_fd=fd,
        reap_poll_sec=0.05,
        log_level="INFO",
        modules=("fake_channel_tool",),
    )

    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "boba.sandbox.guest", *args.render()],
        env=env,
        pass_fds=(fd,),
        stdin=subprocess.DEVNULL,
    )


def _broken_spawner(fd: int) -> subprocess.Popen[bytes]:
    """Процесс, умирающий сразу: старт никогда не станет ready."""
    return subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        pass_fds=(fd,),
        stdin=subprocess.DEVNULL,
    )


def _call_echo(
    supervisor: ZygoteSupervisor,
    text: str,
    *,
    isolate: bool = False,
    timeout_sec: float = 30.0,
    call_id: str = "t-1",
) -> tuple[Any, Recorder, Recorder]:
    """Вызов fx_echo через зиготу; возвращает (outcome, result, stderr)."""
    result = Recorder()
    stderr = Recorder()
    stdout = Recorder()

    stdin = json.dumps({"cfg": CFG.revealed()}).encode("utf-8")

    outcome = supervisor.call(
        call_id,
        ["fx_echo", "--text", text],
        stdin,
        NO_LIMITS,
        {
            ToolChannel.RESULT: result.feed,
            ToolChannel.STDERR: stderr.feed,
            ToolChannel.STDOUT: stdout.feed,
        },
        isolate=isolate,
        mounts=CallMounts(proc="/proc", tmp="/tmp", tmp_bytes=16 * 1024 * 1024),  # noqa: S108
        timeout_sec=timeout_sec,
        kill_grace_sec=5.0,
    )
    return outcome, result, stderr


@pytest.fixture
def supervisor() -> Any:
    born: list[ZygoteSupervisor] = []

    def make(
        spawner: Any,
        policy: ZygotePolicy = FAST,
        warmup: Any = WARMUP_CALLS,
    ) -> ZygoteSupervisor:
        instance = ZygoteSupervisor(
            "test",
            spawner,
            policy,
            stderr_tail_bytes=4096,
            warmup_calls=warmup,
        )
        born.append(instance)
        return instance

    yield make

    for instance in born:
        instance.stop()


class TestLifecycle:
    def test_start_and_simple_call(self, supervisor: Any) -> None:
        zygote = supervisor(_plain_spawner)
        zygote.start()

        if zygote.state is not ZygoteState.READY:
            raise AssertionError(f"state={zygote.state}")

        outcome, result, _ = _call_echo(zygote, "hello")

        if outcome.exit_code != 0:
            raise AssertionError(f"rc={outcome.exit_code}")

        reply = REPLY.validate_json(bytes(result.data))
        if not isinstance(reply, ReplyOk):
            raise AssertionError(f"reply={reply}")

        if "hello" not in reply.content:
            raise AssertionError(f"content={reply.content!r}")

    def test_sudden_death_restarts(self, supervisor: Any) -> None:
        """Зигота убита SIGKILL — супервизор перезапускает, вызовы работают."""
        zygote = supervisor(_plain_spawner)
        zygote.start()

        first_pid = zygote._proc.pid
        os.kill(first_pid, signal.SIGKILL)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if zygote.state is ZygoteState.READY and zygote._proc.pid != first_pid:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"не перезапустилась: state={zygote.state}")

        outcome, result, _ = _call_echo(zygote, "after-restart")

        if outcome.exit_code != 0:
            raise AssertionError(f"rc={outcome.exit_code}")

        if "after-restart" not in bytes(result.data).decode():
            raise AssertionError("вызов после рестарта не отработал")

    def test_never_starting_exhausts_attempts(self, supervisor: Any) -> None:
        """Старт честно повторяется и упирается в лимит попыток."""
        zygote = supervisor(_broken_spawner)

        started = time.monotonic()
        with pytest.raises(Exception, match="not ready after 3"):
            zygote.start()

        if zygote.state is not ZygoteState.FAILED:
            raise AssertionError(f"state={zygote.state}")

        # три попытки с backoff, но не вечность
        took = time.monotonic() - started
        if took > FAST.start_timeout_sec * FAST.max_start_attempts + 5:
            raise AssertionError(f"попытки шли слишком долго: {took:.1f}s")

    def test_call_on_failed_zygote_is_an_error(self, supervisor: Any) -> None:
        """Инструмент при недоступной зиготе падает ошибкой, без деградации."""
        zygote = supervisor(_broken_spawner)

        with pytest.raises(Exception, match="not ready"):
            zygote.start()

        with pytest.raises(ZygoteUnavailableError):
            _call_echo(zygote, "must-fail")

    def test_call_on_stopped_zygote_is_an_error(self, supervisor: Any) -> None:
        zygote = supervisor(_plain_spawner)
        zygote.start()
        zygote.stop()

        with pytest.raises(ZygoteUnavailableError):
            _call_echo(zygote, "must-fail")

    def test_death_mid_call_raises(self, supervisor: Any) -> None:
        """Зигота умерла посреди вызова: ошибка вызова, не зависание."""
        zygote = supervisor(_plain_spawner)
        zygote.start()

        result = Recorder()
        proc = zygote._proc

        def killer() -> None:
            time.sleep(0.5)
            proc.kill()

        threading.Thread(target=killer, daemon=True).start()

        with pytest.raises(ZygoteCallError, match=r"died mid-call|control closed"):
            zygote.call(
                "t-die",
                ["fx_echo", "--text", "slow", "--sleep-sec", "30"],
                json.dumps({"cfg": CFG.revealed()}).encode(),
                NO_LIMITS,
                {ToolChannel.RESULT: result.feed},
                isolate=False,
                mounts=CallMounts(proc="/proc", tmp="/tmp", tmp_bytes=16 * 1024 * 1024),  # noqa: S108
                timeout_sec=30.0,
                kill_grace_sec=5.0,
            )

    def test_restart_after_death_mid_call(self, supervisor: Any) -> None:
        """После смерти посреди вызова зигота возвращается в строй."""
        zygote = supervisor(_plain_spawner)
        zygote.start()

        first_pid = zygote._proc.pid
        zygote._proc.kill()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reborn = zygote._proc
            if (
                zygote.state is ZygoteState.READY
                and reborn is not None
                and reborn.pid != first_pid
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"не перезапустилась: state={zygote.state}")

        outcome, _, _ = _call_echo(zygote, "recovered")
        if outcome.exit_code != 0:
            raise AssertionError(f"rc={outcome.exit_code}")


class TestCall:
    def test_expected_failure_envelope(self, supervisor: Any) -> None:
        """Ожидаемый отказ тела едет конвертом error, rc=1."""
        zygote = supervisor(_plain_spawner)
        zygote.start()

        outcome, result, _ = _call_echo(zygote, "boom")

        if outcome.exit_code != 1:
            raise AssertionError(f"rc={outcome.exit_code}")

        reply = json.loads(bytes(result.data))
        if reply["status"] != "error":
            raise AssertionError(f"reply={reply}")

    def test_timeout_kills_executor(self, supervisor: Any) -> None:
        zygote = supervisor(_plain_spawner)
        zygote.start()

        started = time.monotonic()
        outcome, _, _ = _call_echo(
            zygote, "sleepy", timeout_sec=1.0, call_id="t-timeout"
        )
        took = time.monotonic() - started

        if not outcome.timed_out:
            raise AssertionError("timed_out должен быть выставлен")

        if took > 10:
            raise AssertionError(f"убийство по таймауту заняло {took:.1f}s")

        # зигота жива и обслуживает следующий вызов
        after, _result, _ = _call_echo(zygote, "still-alive")
        if after.exit_code != 0:
            raise AssertionError(f"rc={after.exit_code}")

    def test_cancellation_kills_executor(self, supervisor: Any) -> None:
        zygote = supervisor(_plain_spawner)
        zygote.start()

        with run_cancellation() as cancellation:

            def cancel_soon() -> None:
                time.sleep(0.7)
                cancellation.cancel()

            threading.Thread(target=cancel_soon, daemon=True).start()

            with pytest.raises(ToolStopped):
                _call_echo(zygote, "slow", call_id="t-cancel", timeout_sec=30.0)

    def test_parallel_calls(self, supervisor: Any) -> None:
        zygote = supervisor(_plain_spawner)
        zygote.start()

        def one(index: int) -> str:
            _, result, _ = _call_echo(zygote, f"msg-{index}", call_id=f"t-par-{index}")
            reply = REPLY.validate_json(bytes(result.data))
            if not isinstance(reply, ReplyOk):
                raise AssertionError(f"reply={reply}")
            return reply.content

        with ThreadPoolExecutor(4) as pool:
            texts = list(pool.map(one, range(4)))

        for index, text in enumerate(texts):
            if f"msg-{index}" not in text:
                raise AssertionError(f"перепутаны ответы: {index} -> {text!r}")


@needs_bwrap
@needs_userns
class TestIsolated:
    """E2e в настоящем bwrap: зигота с CAP_SYS_ADMIN, дети изолированы."""

    def _bwrap_spawner(self, fd: int) -> subprocess.Popen[bytes]:
        python_path = SandboxStand.python_path(
            "/usr/src/infra/sandbox/boba-sandbox/tests"
        )

        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise AssertionError("bwrap исчез после skipif")

        argv = [
            bwrap,
            "--die-with-parent",
            "--unshare-user",
            "--uid",
            "0",
            "--gid",
            "0",
            "--cap-add",
            "CAP_SYS_ADMIN",
            # исполнитель сбрасывает им bounding set тела, как в боевой цепочке
            "--cap-add",
            "CAP_SETPCAP",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-net",
            "--hostname",
            "zygote",
            "--new-session",
            "--ro-bind",
            str(ROOTFS),
            "/",
            "--ro-bind",
            str(SANDBOX / "third" / "python"),
            "/usr/local",
            "--ro-bind",
            str(SANDBOX / "site"),
            "/usr/local/lib/python3.11/site-packages",
            "--ro-bind",
            str(REPO / "packages"),
            "/usr/src",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # noqa: S108
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "PYTHONPATH",
            python_path,
            "--setenv",
            "HOME",
            "/tmp",  # noqa: S108
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--",
            "python3",
            "-m",
            "boba.sandbox.guest",
            *ZygoteArgs(
                socket_fd=fd,
                reap_poll_sec=0.05,
                log_level="INFO",
                modules=("fake_channel_tool",),
            ).render(),
        ]

        return subprocess.Popen(  # noqa: S603
            argv, pass_fds=(fd,), stdin=subprocess.DEVNULL
        )

    def test_isolated_call_works(self, supervisor: Any) -> None:
        zygote = supervisor(self._bwrap_spawner)
        zygote.start()

        outcome, result, _ = _call_echo(zygote, "isolated", isolate=True)

        if outcome.exit_code != 0:
            raise AssertionError(f"rc={outcome.exit_code}")

        reply = REPLY.validate_json(bytes(result.data))
        if not isinstance(reply, ReplyOk):
            raise AssertionError(f"reply={reply}")

        if outcome.child_pid <= 0:
            raise AssertionError("host-pid исполнителя не получен")

    def test_children_do_not_share_tmp(self, supervisor: Any) -> None:
        """Параллельные вызовы не видят /tmp друг друга."""
        zygote = supervisor(self._bwrap_spawner)
        zygote.start()

        def probe(index: int) -> str:
            result = Recorder()
            outcome = zygote.call(
                f"t-tmp-{index}",
                ["fx_probe_tmp", "--marker", f"m{index}"],
                json.dumps({"cfg": CFG.revealed()}).encode(),
                NO_LIMITS,
                {ToolChannel.RESULT: result.feed},
                isolate=True,
                mounts=CallMounts(proc="/proc", tmp="/tmp", tmp_bytes=16 * 1024 * 1024),  # noqa: S108
                timeout_sec=30.0,
                kill_grace_sec=5.0,
            )
            if outcome.exit_code != 0:
                raise AssertionError(f"rc={outcome.exit_code}")

            reply = REPLY.validate_json(bytes(result.data))
            if not isinstance(reply, ReplyOk):
                raise AssertionError(f"reply={reply}")
            return reply.content

        with ThreadPoolExecutor(2) as pool:
            contents = list(pool.map(probe, range(2)))

        for index, content in enumerate(contents):
            seen = json.loads(content)
            if seen["markers"] != [f"m{index}"]:
                raise AssertionError(f"вызов {index} видит чужие файлы: {seen}")

            if seen["init"] != "python3" or seen["pid"] > 8:
                raise AssertionError(f"тело не в своём pid ns: {seen}")

            if int(seen["cap_eff"], 16) != 0:
                raise AssertionError(f"capabilities не сброшены: {seen}")
