"""Регрессии дефектов, найденных при переводе запуска инструментов на зиготу.

Каждый тест воспроизводит свой дефект целиком, на настоящей цепочке запуска:
поднимается зигота секции, выполняется вызов, наблюдается внешнее состояние
хоста. Если правка откатится, тест упадёт по той же причине, по которой дефект
был найден.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from fake_channel_tool import ChannelConfig, fx_chatter
from pydantic import SecretStr
from zygote_stand import ROOTFS, ROOTFS_IMAGE, SandboxStand, ZygoteStand

from boba.sandbox import SandboxProfile
from boba.sandbox.zygote import ZygoteRegistry, ZygoteSpawner, ZygoteState
from boba.toolkit.channels import JournalChannel, ToolChannel
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolMain
from boba.toolkit.images import PartialCopy
from boba.toolkit.launcher import LauncherError
from boba.toolkit.protocol import ReplyOk, ToolCommand
from boba.toolkit.stream import (
    ChannelSinks,
    Chunk,
    StreamSink,
    ToolChannelsTap,
)
from boba.toolkit.zygote import WarmupCall

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)

pytestmark = [needs_sandbox, needs_userns]

MODULE = "fake_channel_tool"
"""Модуль инструментов стенда: его тела запускает зигота."""

WARMUP: tuple[WarmupCall, ...] = (
    WarmupCall(module=MODULE, hook="warm_cache", config={"greeting": "privet"}),
)
"""Прогрев объявлен хуком модуля: без конфига зигота не стартует."""

CFG = ChannelConfig(token=SecretStr("defect-s3cret"))


class Waiting:
    """Сколько ждать внешнего состояния хоста."""

    SETTLE_SEC: ClassVar[float] = 15.0
    POLL_SEC: ClassVar[float] = 0.1
    ALIVE_SEC: ClassVar[float] = 2.0
    """Пауза, за которую сработал бы pdeathsig умершего треда-родителя."""


class ProcTree:
    """Дерево процессов хоста: потомки pid'а по /proc.

    Исполнитель вызова рождается не прямым ребёнком того, кого запустил
    супервизор: между ними внешний bwrap, вложенный bwrap и сама зигота.
    Поэтому считаем всех потомков, а не первый уровень.
    """

    PROC: ClassVar[str] = "/proc"

    @classmethod
    def children_of(cls, pid: int) -> frozenset[int]:
        kids: set[int] = set()
        task_dir = os.path.join(cls.PROC, str(pid), "task")

        try:
            tids = os.listdir(task_dir)
        except OSError:
            return frozenset()

        for tid in tids:
            path = os.path.join(task_dir, tid, "children")
            try:
                with open(path) as handle:
                    raw = handle.read()
            except OSError:
                continue

            for item in raw.split():
                kids.add(int(item))

        return frozenset(kids)

    @classmethod
    def descendants_of(cls, pid: int) -> frozenset[int]:
        found: set[int] = set()
        pending = [pid]

        while pending:
            current = pending.pop()
            for child in cls.children_of(current):
                if child in found:
                    continue

                found.add(child)
                pending.append(child)

        return frozenset(found)

    @classmethod
    def alive(cls, pid: int) -> bool:
        return os.path.exists(os.path.join(cls.PROC, str(pid)))

    @classmethod
    def wait_tree_settled(cls, pid: int, known: frozenset[int]) -> frozenset[int]:
        """Ждёт, пока в дереве зиготы не останется процессов сверх известных."""
        deadline = time.monotonic() + Waiting.SETTLE_SEC

        while time.monotonic() < deadline:
            extra = cls.descendants_of(pid) - known
            if not extra:
                return frozenset()

            time.sleep(Waiting.POLL_SEC)

        return cls.descendants_of(pid) - known


class ChannelRecorder(StreamSink):
    """Приёмник канала журнала: копит всё, что пришло."""

    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, data: Chunk) -> None:
        self.data.extend(data)

    def feed_text(self, text: str) -> None:
        self.data.extend(text.encode("utf-8"))

    def text(self) -> str:
        return bytes(self.data).decode("utf-8")


class RecordingSinks(ChannelSinks):
    """Журнал вызова: приёмник на канал."""

    def __init__(self) -> None:
        self.channels: dict[JournalChannel, ChannelRecorder] = {}

    def sink_of(self, channel: JournalChannel) -> StreamSink:
        return self.channels.setdefault(channel, ChannelRecorder())

    def text_of(self, channel: JournalChannel) -> str:
        recorder = self.channels.get(channel)
        if recorder is None:
            return ""

        return recorder.text()


class BrokenSink(StreamSink):
    """Приёмник, который срывается на первом же куске вывода."""

    FAILURE: ClassVar[str] = "consumer is broken"

    def feed(self, data: Chunk) -> None:
        raise RuntimeError(self.FAILURE)

    def feed_text(self, text: str) -> None:
        raise RuntimeError(self.FAILURE)


class BrokenSinks(ChannelSinks):
    """Журнал вызова, у которого сломан любой канал."""

    def sink_of(self, channel: JournalChannel) -> StreamSink:
        return BrokenSink()


def _tool_env() -> dict[str, str]:
    """Окружение профиля: к путям стенда добавлен каталог с fake_channel_tool."""
    tests_dir = "/usr/src/infra/sandbox/boba-sandbox/tests"
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": SandboxStand.python_path(tests_dir),
        "HOME": "/tmp",  # noqa: S108
        "LANG": "C.UTF-8",
    }


def _profile(**overrides: Any) -> SandboxProfile:
    return SandboxStand.profile(env=_tool_env(), **overrides)


def _command(name: str, arguments: dict[str, Any]) -> ToolCommand:
    """Команда модуля ровно как её строит обёртка запуска инструмента."""
    address = ToolAddress(module=MODULE, name=name)
    schema = ToolArgv.schema_of(ToolMain.toolset(fx_chatter)[0])
    return ToolArgv.render(address, schema, arguments)


@pytest.fixture
def section() -> Iterator[str]:
    """Уникальная секция на тест: зиготы реестра гасятся после него."""
    name = f"defect-{uuid4().hex[:8]}"

    yield name

    ZygoteRegistry.stop_all()


class TestBodyOutputIsNotLost:
    """Дефект: `os._exit` не сбрасывал буферы, и вывод тела пропадал.

    stdout тела уходит в пайп, а значит буферизован блоками: короткая печать
    без flush не доезжала до журнала вовсе.
    """

    def test_print_without_flush_reaches_the_journal(self, section: str) -> None:
        caller = ZygoteStand.caller(section, _profile(), [MODULE], warmup_calls=WARMUP)

        sinks = RecordingSinks()
        ToolChannelsTap.set(sinks)
        try:
            outcome = caller.run_tool(_command("fx_chatter", {}))
        finally:
            ToolChannelsTap.set(None)

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        stdout = sinks.text_of(ToolChannel.STDOUT)
        if "print line from the body" not in stdout:
            raise AssertionError(f"печать тела потеряна: stdout={stdout!r}")


class TestBodyLogLevel:
    """Дефект: уровень логера приложения перестал доезжать до песочницы.

    Он ехал переменной окружения из удалённого раннера; без него тело всегда
    работало на INFO, и секция logger конфига на песочницу не влияла.
    """

    @staticmethod
    def _stdout_at(level: int, section: str) -> str:
        """Вывод тела при заданном уровне логера приложения."""
        app_logger = logging.getLogger(ZygoteSpawner.APP_LOGGER)
        previous = app_logger.level
        app_logger.setLevel(level)

        try:
            caller = ZygoteStand.caller(
                section, _profile(), [MODULE], warmup_calls=WARMUP
            )
            sinks = RecordingSinks()
            ToolChannelsTap.set(sinks)
            try:
                caller.run_tool(_command("fx_chatter", {}))
            finally:
                ToolChannelsTap.set(None)
        finally:
            app_logger.setLevel(previous)

        return sinks.text_of(ToolChannel.STDOUT)

    def test_info_level_lets_the_body_talk(self, section: str) -> None:
        stdout = self._stdout_at(logging.INFO, section)

        if "info line from the body" not in stdout:
            raise AssertionError(f"уровень приложения не доехал: stdout={stdout!r}")

    def test_warning_level_silences_info(self, section: str) -> None:
        stdout = self._stdout_at(logging.WARNING, section)

        if "info line from the body" in stdout:
            raise AssertionError(f"тело пишет ниже уровня приложения: {stdout!r}")

        if "warning line from the body" not in stdout:
            raise AssertionError(f"предупреждение тела потеряно: {stdout!r}")


class TestFailureIsLogged:
    """Дефект: причина падения вызова перестала попадать в журнал.

    Логирование жило в удалённом раннере, и после перевода на зиготу rc=1 в
    журнале не сопровождался ничем — ни причиной, ни хвостом вывода.
    """

    LOGGER: ClassVar[str] = "boba.sandbox.zygote"

    def test_failed_call_logs_reason_and_tail(
        self, section: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        caller = ZygoteStand.caller(section, _profile())

        with caplog.at_level(logging.WARNING, logger=self.LOGGER):
            outcome = caller.call_text("echo boom >&2; exit 3", stdin="")

        if outcome.result.exit_code != 3:
            raise AssertionError(f"rc={outcome.result.exit_code}")

        messages: list[str] = []
        for record in caplog.records:
            messages.append(record.getMessage())

        failures: list[str] = []
        for message in messages:
            if "failed (rc=3)" in message:
                failures.append(message)

        if not failures:
            raise AssertionError(f"причина падения не в журнале: {messages}")

        if "boom" not in failures[0]:
            raise AssertionError(f"хвост вывода не в журнале: {failures[0]!r}")


class TestBrokenSinkFreesTheCall:
    """Дефект: сбой приёмника вывода оставлял исполнителя жить.

    Исключение уходило наверх, а исполнитель со своим fuse2fs продолжал
    держать образ пользователя до конца жизни зиготы.
    """

    def test_failing_sink_kills_the_executor(self, section: str) -> None:
        caller = ZygoteStand.caller(section, _profile())
        caller.call_text("echo warm", stdin="")

        zygote_pid = caller.supervisor.pid
        if zygote_pid == 0:
            raise AssertionError("зигота не запущена")

        known = ProcTree.descendants_of(zygote_pid)

        ToolChannelsTap.set(BrokenSinks())
        try:
            with pytest.raises(RuntimeError, match=BrokenSink.FAILURE):
                caller.call_text("echo noise; sleep 300", stdin="")
        finally:
            ToolChannelsTap.set(None)

        survivors = ProcTree.wait_tree_settled(zygote_pid, known)
        if survivors:
            raise AssertionError(f"исполнитель пережил сбой приёмника: {survivors}")

        again = caller.call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError(f"секция сломана: rc={again.result.exit_code}")


class TestConcurrentStart:
    """Дефект: `start()` возвращался сразу, если зиготу поднимал другой поток.

    Второй поток уходил вызовом в ещё не готовую зиготу и получал
    ZygoteUnavailableError вместо результата.
    """

    THREADS: ClassVar[int] = 6

    def test_parallel_first_calls_all_succeed(self, section: str) -> None:
        profile = _profile()

        def call(index: int) -> int:
            caller = ZygoteStand.caller(section, profile)
            outcome = caller.call_text(f"echo {index}", stdin="")
            return outcome.result.exit_code

        with ThreadPoolExecutor(max_workers=self.THREADS) as pool:
            futures = []
            for index in range(self.THREADS):
                futures.append(pool.submit(call, index))

            codes = []
            for future in futures:
                codes.append(future.result(timeout=Waiting.SETTLE_SEC * 4))

        if codes != [0] * self.THREADS:
            raise AssertionError(f"часть вызовов не дошла до зиготы: {codes}")


class TestZygoteOutlivesSpawningThread:
    """Дефект: зигота умирала вместе с породившим её тредом.

    `--die-with-parent` держится на pdeathsig, а он привязан к треду: подъём
    из временного треда (пул, обработчик запроса) убивал зиготу сразу после
    завершения этого треда.
    """

    def test_zygote_survives_the_pool_thread(self, section: str) -> None:
        profile = _profile()

        pool = ThreadPoolExecutor(max_workers=1)
        caller = pool.submit(ZygoteStand.caller, section, profile).result()
        pool.shutdown(wait=True)

        born = caller.supervisor.pid
        if born == 0:
            raise AssertionError("зигота не запущена")

        time.sleep(Waiting.ALIVE_SEC)

        if not ProcTree.alive(born):
            raise AssertionError("зигота убита смертью породившего треда")

        if caller.supervisor.pid != born:
            raise AssertionError("зигота перезапущена: значит умирала")

        if caller.supervisor.state is not ZygoteState.READY:
            raise AssertionError(f"состояние={caller.supervisor.state}")

        outcome = caller.call_text("echo alive", stdin="")
        if outcome.result.exit_code != 0:
            raise AssertionError(f"rc={outcome.result.exit_code}")


class TestRootMountRecovery:
    """Демон корня секции умер: вызовы бессмысленны, секция поднимается заново.

    Ровно этот случай в проде выглядел как бесконечные rc=126 у одной секции:
    зигота жива, а её fuse2fs мёртв, и каждый вызов падал на mount(/proc) с
    ENOTCONN. Хост обязан это распознать, перезапустить секцию и написать в
    журнал, что именно восстанавливалось.
    """

    needs_image = pytest.mark.skipif(
        not ROOTFS_IMAGE.exists(),
        reason="нет rootfs.ext4 (собрать: make sandbox-image)",
    )

    LOGGER: ClassVar[str] = "boba.sandbox.zygote"
    RESTART_SEC: ClassVar[float] = 30.0

    @staticmethod
    def _fuse_of(zygote_pid: int) -> int:
        """Pid демона корня: он рождается ребёнком той же цепочки, что и зигота."""
        for pid in ProcTree.descendants_of(zygote_pid):
            try:
                with open(f"/proc/{pid}/comm") as comm:
                    name = comm.read().strip()
            except OSError:
                continue

            if name == "fuse2fs":
                return pid

        return 0

    def _await_restart(self, caller: Any, born: int) -> int:
        deadline = time.monotonic() + self.RESTART_SEC
        while time.monotonic() < deadline:
            current = caller.supervisor.pid
            if current and current != born:
                return current

            time.sleep(Waiting.POLL_SEC)

        return caller.supervisor.pid

    @needs_image
    def test_dead_root_daemon_restarts_the_section(
        self, section: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        profile = _profile(rootfs=str(ROOTFS_IMAGE))
        caller = ZygoteStand.caller(section, profile)

        warm = caller.call_text("echo warm", stdin="")
        if warm.result.exit_code != 0:
            raise AssertionError(f"rc={warm.result.exit_code}")

        born = caller.supervisor.pid
        daemon = self._fuse_of(born)
        if daemon == 0:
            raise AssertionError("демон корня не найден в дереве зиготы")

        with caplog.at_level(logging.INFO, logger=self.LOGGER):
            os.kill(daemon, signal.SIGKILL)

            with pytest.raises(LauncherError):
                caller.call_text("echo after-kill", stdin="")

            restarted = self._await_restart(caller, born)

        if restarted == born:
            raise AssertionError("секция не перезапущена после смерти демона корня")

        again = caller.call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError(f"секция не обслуживает вызовы: {again.result!r}")

        messages: list[str] = []
        for record in caplog.records:
            messages.append(record.getMessage())

        journal = "\n".join(messages)
        for phase in ("root mount is gone", "detected:", "recovery started", "ready:"):
            if phase not in journal:
                raise AssertionError(f"в журнале нет {phase!r}:\n{journal}")


class TestCallFdsAreNotInherited:
    """Дефект: тело вызова наследовало дескриптор своего cgroup-leaf'а.

    Рождение исполнителя прямо в leaf'е (clone3) требует открытого каталога
    группы, и он переживал вход в тело: скрипт инструмента читал через него
    memory.max, снимал себе лимит записью и ходил по соседним группам.
    """

    CGROUP_BASE: ClassVar[str] = "/sys/fs/cgroup/boba"

    PROBE: ClassVar[str] = """
for fd in /proc/self/fd/*; do
  readlink "$fd"
done
"""
    """Тело печатает, куда указывает каждый его дескриптор."""

    needs_cgroup = pytest.mark.skipif(
        not os.access(CGROUP_BASE, os.W_OK),
        reason="нет делегированного /sys/fs/cgroup/boba (прогнать cgroup-init.sh)",
    )

    @needs_cgroup
    def test_body_has_no_cgroup_descriptor(self, section: str) -> None:
        profile = _profile(
            cgroup_base=self.CGROUP_BASE,
            group_memory_bytes=512 * 1024 * 1024,
            group_swap_bytes=0,
            group_pids_max=64,
        )
        caller = ZygoteStand.caller(section, profile)

        outcome = caller.call_text(self.PROBE, stdin="")
        if outcome.result.exit_code != 0:
            raise AssertionError(f"rc={outcome.result.exit_code}")

        targets: list[str] = []
        for line in outcome.result.stdout.splitlines():
            if not line.strip():
                continue

            targets.append(line.strip())

        leaked: list[str] = []
        for target in targets:
            if "cgroup" not in target:
                continue

            leaked.append(target)

        if leaked:
            raise AssertionError(f"телу достался дескриптор cgroup: {leaked}")

    @needs_cgroup
    def test_body_cannot_lift_its_memory_limit(self, section: str) -> None:
        """Через дескриптор leaf'а лимит снимался записью в memory.max."""
        limit = 512 * 1024 * 1024
        profile = _profile(
            cgroup_base=self.CGROUP_BASE,
            group_memory_bytes=limit,
            group_swap_bytes=0,
            group_pids_max=64,
        )
        caller = ZygoteStand.caller(section, profile)

        attack = """
for fd in /proc/self/fd/*; do
  target=$(readlink "$fd")
  case "$target" in
    */boba/run-*) echo max > "$fd/memory.max" && echo lifted ;;
  esac
done
echo done
"""
        outcome = caller.call_text(attack, stdin="")
        if "lifted" in outcome.result.stdout:
            raise AssertionError("тело сняло себе групповой лимит памяти")


class TestPartialCopyCleanup:
    """Дефект: владельца частичной копии образа искали по pid из её имени.

    У исполнителя вызова свой pid namespace, и в нём он всегда pid 1, а
    /proc — свой: проверка «владелец жив» отвечала «жив» на любой мусор с
    таким именем, и брошенная копия оставалась лежать навсегда.
    """

    OWN_PID: ClassVar[int] = 1
    """Pid исполнителя внутри его pid namespace: он же в имени его копии."""

    USER: ClassVar[str] = "7"

    needs_mkfs = pytest.mark.skipif(
        shutil.which("mkfs.ext4") is None, reason="нет mkfs.ext4 для шаблона образа"
    )

    @needs_mkfs
    def test_copy_named_by_the_namespace_pid_is_removed(
        self, section: str, tmp_path: Path
    ) -> None:
        """Мусор подкладывается к готовому образу: свою копию вызов не делает.

        Иначе материализация переписала бы файл с тем же именем сама, и
        разницы между рабочей уборкой и сломанной было бы не видно.
        """
        profile = SandboxStand.image_profile(tmp_path, env=_tool_env())

        workspace = profile.mounts.workspace
        if workspace is None:
            raise AssertionError("профиль стенда без секции workspace")

        caller = ZygoteStand.caller(
            section, profile, path_vars=lambda: {"user_id": self.USER}
        )
        first = caller.call_text("echo warm", stdin="")
        if first.result.exit_code != 0:
            raise AssertionError(f"rc={first.result.exit_code}")

        image = Path(workspace.image_of(self.USER))
        if not image.exists():
            raise AssertionError("образ пользователя не собран")

        partial = Path(PartialCopy.render(str(image), self.OWN_PID))
        partial.write_bytes(b"copy in progress")

        second = caller.call_text("echo alive", stdin="")
        if second.result.exit_code != 0:
            raise AssertionError(f"rc={second.result.exit_code}")

        if partial.exists():
            raise AssertionError("брошенная копия осталась на месте")
