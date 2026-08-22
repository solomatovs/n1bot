"""Тесты инструментов: bash в bwrap-песочнице и сборка её argv."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from boba.chainlit.agent.tools import BashToolConfig, build_bash_tool
from boba.chainlit.infra.plugins import as_structured_tool
from boba.sandbox.argv import build_zygote_argv
from boba.sandbox.profile import (
    BindSpec,
    SandboxProfile,
    SandboxToolConfig,
)
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.toolkit.launcher import LauncherError
from boba.toolkit.result import ShellResult


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _tool_call(name: str, args: dict) -> dict:
    return {"args": args, "id": f"call-{name}", "name": name, "type": "tool_call"}


_PROFILE_BASE: dict[str, object] = {
    "host": {
        "mounting": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
        "stderr_tail_bytes": 4096,
        "channel_limit_bytes": 67108864,
        "fail_tail_chars": 2000,
        "kill_grace_sec": 5,
        "cgroup_base": "",
    },
    "rootfs": {
        "dir": "",
    },
    "mounts": {
        "setup_ro": (),
        "setup_rw": (),
        "ro": (),
        "rw": (),
        "images": (),
        "image_template": "",
        "tmpfs": ("/tmp:64M",),  # noqa: S108
        "proc": "/proc",
        "dev": "/dev",
        "call_tmpfs": "/tmp",  # noqa: S108
    },
    "isolation": {
        "reap_poll_sec": 0.05,
        "network": False,
        "env": {},
        "max_processes": 256,
    },
    "limits": {
        "timeout_sec": 30,
        "process_memory_bytes": 512 * 1024 * 1024,
        "process_cpu_sec": 30,
        "process_file_bytes": 64 * 1024 * 1024,
        "process_open_files": 256,
        "process_oom_score_adj": 0,
    },
    "run": {
        "shell": "/bin/bash",
        "cwd": "",
    },
}


def _place(raw: dict[str, Any], name: str, value: Any) -> None:
    """Плоское поле профиля в свою группу: группа находится по модели."""
    if name in SandboxProfile.GROUPS:
        group = dict(raw.get(name, {}))
        if isinstance(value, dict):
            group.update(value)
            raw[name] = group
            return

        raw[name] = value
        return

    for group_name in SandboxProfile.GROUPS:
        model = SandboxProfile.model_fields[group_name].annotation
        if name not in getattr(model, "model_fields", {}):
            continue

        group = dict(raw.get(group_name, {}))
        group[name] = value
        raw[group_name] = group
        return

    msg = f"профиль: поле {name!r} не принадлежит ни одной группе"
    raise KeyError(msg)


def _merged(base: dict[str, Any], flat: dict[str, Any]) -> dict[str, Any]:
    """Копия базы профиля с наложенными плоскими полями."""
    raw: dict[str, Any] = {}
    for name, value in base.items():
        if isinstance(value, dict):
            raw[name] = dict(value)
            continue

        raw[name] = value

    for name, value in flat.items():
        _place(raw, name, value)

    return raw


def _profile(**kw: Any) -> SandboxProfile:
    """Все поля профиля обязательны; база даёт валидный минимум для тестов."""
    return SandboxProfile.model_validate(_merged(_PROFILE_BASE, kw))


_HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")

_VENV = Path(__file__).resolve().parents[4] / ".venv"
_PACKAGES = Path(__file__).resolve().parents[3]

_PYTHON_BINDS = (str(_VENV), str(_PACKAGES))
"""Зиготе нужен интерпретатор с пакетами: на хостовом корне это venv стенда."""

_SANDBOX_ENV = {
    "PATH": f"{_VENV}/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",  # noqa: S108
    "LANG": "C.UTF-8",
}

_ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


class TestProfileValidation:
    def test_zero_memory_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="process_memory_bytes"):
            _profile(process_memory_bytes=0)

    def test_zero_cpu_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="process_cpu_sec"):
            _profile(process_cpu_sec=0)

    def test_zero_file_size_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="process_file_bytes"):
            _profile(process_file_bytes=0)

    def test_zero_open_files_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="process_open_files"):
            _profile(process_open_files=0)

    def test_zero_process_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_processes"):
            _profile(max_processes=0)

    def test_all_fields_required(self) -> None:
        with pytest.raises(ValueError, match="Field required"):
            SandboxProfile.model_validate({})


class TestBwrapArgv:
    """Юниты pure-builder'а argv: не требуют установленного bwrap."""

    _WS = "/srv/workspace"

    def test_starts_with_bwrap_and_unshare_flags(self) -> None:
        argv = build_zygote_argv(_profile(), ["echo", "hi"], env={})
        if not (argv[0].endswith("bwrap")):
            raise AssertionError('argv[0].endswith("bwrap")')
        if "--die-with-parent" not in argv:
            raise AssertionError('"--die-with-parent" in argv')
        if "--unshare-user" not in argv:
            raise AssertionError('"--unshare-user" in argv')
        if "--unshare-pid" not in argv:
            raise AssertionError('"--unshare-pid" in argv')
        if "--new-session" not in argv:
            raise AssertionError('"--new-session" in argv')

    def test_userns_creation_is_closed_by_capability(self) -> None:
        """Вложенные userns закрывает сама зигота: ей нужен CAP_SYS_RESOURCE."""
        argv = build_zygote_argv(_profile(), ["true"], env={})

        caps: list[str] = []
        for index, arg in enumerate(argv):
            if arg == "--cap-add":
                caps.append(argv[index + 1])

        if "CAP_SYS_RESOURCE" not in caps:
            raise AssertionError('"CAP_SYS_RESOURCE" in caps')

    def test_neutral_hostname(self) -> None:
        argv = build_zygote_argv(_profile(), ["true"], env={})
        if argv[argv.index("--hostname") + 1] != "sandbox":
            raise AssertionError('argv[argv.index("--hostname") + 1] == "sandbox"')

    def test_network_disabled_adds_unshare_net(self) -> None:
        argv = build_zygote_argv(_profile(network=False), ["true"], env={})
        if "--unshare-net" not in argv:
            raise AssertionError('"--unshare-net" in argv')

    def test_network_enabled_omits_unshare_net(self) -> None:
        argv = build_zygote_argv(_profile(network=True), ["true"], env={})
        if "--unshare-net" in argv:
            raise AssertionError('"--unshare-net" not in argv')

    def test_no_implicit_rw_binds(self) -> None:
        argv = build_zygote_argv(_profile(), ["true"], env={})
        if "--bind-try" in argv:
            raise AssertionError('"--bind-try" not in argv')
        if "--bind" in argv:
            raise AssertionError('"--bind" not in argv')

    def test_rw_bind_same_path(self) -> None:
        profile = _profile(rw=(self._WS,), cwd=self._WS)
        argv = build_zygote_argv(profile, ["true"], env={})
        i = argv.index("--bind-try")
        if argv[i + 1 : i + 3] != [self._WS, self._WS]:
            raise AssertionError("argv[i + 1 : i + 3] == [self._WS, self._WS]")

    def test_rw_bind_with_explicit_target(self) -> None:
        profile = _profile(rw=(f"{self._WS}:/workspace",), cwd="/workspace")
        argv = build_zygote_argv(profile, ["true"], env={})
        i = argv.index("--bind-try")
        if argv[i + 1 : i + 3] != [self._WS, "/workspace"]:
            raise AssertionError('argv[i + 1 : i + 3] == [self._WS, "/workspace"]')

    def test_zygote_starts_at_root(self) -> None:
        """cwd профиля — дело вызова: точку образа монтирует исполнитель."""
        profile = _profile(rw=(f"{self._WS}:/workspace",), cwd="/workspace")
        argv = build_zygote_argv(profile, ["true"], env={})
        if argv[argv.index("--chdir") + 1] != "/":
            raise AssertionError('argv[argv.index("--chdir") + 1] == "/"')

    def test_tmpfs_without_size_rejected(self) -> None:
        """Размер обязателен: неявного «без лимита» больше нет."""
        with pytest.raises(ValueError, match="size is required"):
            _profile(tmpfs=("/tmp",))  # noqa: S108

    def test_tmpfs_size_precedes_mount(self) -> None:
        argv = build_zygote_argv(_profile(tmpfs=("/tmp:64M",)), ["true"], env={})  # noqa: S108
        i = argv.index("--size")
        if argv[i + 1] != str(64 * 1024**2):
            raise AssertionError("argv[i + 1] == str(64 * 1024**2)")
        if argv[i + 2 : i + 4] != ["--tmpfs", "/tmp"]:  # noqa: S108
            raise AssertionError('argv[i + 2 : i + 4] == ["--tmpfs", "/tmp"]')

    def test_tmpfs_bad_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid size"):
            _profile(tmpfs=("/tmp:64X",))  # noqa: S108

    def test_rootfs_mounted_as_root_before_proc_dev(self) -> None:
        argv = build_zygote_argv(
            _profile(rootfs={"dir": "/srv/rootfs"}, ro=()),
            ["true"],
            env={},
        )
        i = argv.index("--ro-bind")
        if argv[i + 1 : i + 3] != ["/srv/rootfs", "/"]:
            raise AssertionError('argv[i + 1 : i + 3] == ["/srv/rootfs", "/"]')
        if i >= argv.index("--proc"):
            raise AssertionError('i < argv.index("--proc")')
        if i >= argv.index("--dev"):
            raise AssertionError('i < argv.index("--dev")')

    def test_env_cleared_and_set(self) -> None:
        argv = build_zygote_argv(
            _profile(),
            ["true"],
            env={"PATH": "/usr/bin:/bin"},
        )
        if "--clearenv" not in argv:
            raise AssertionError('"--clearenv" in argv')
        i = argv.index("--setenv")
        if argv[i + 1 : i + 3] != ["PATH", "/usr/bin:/bin"]:
            raise AssertionError('argv[i + 1 : i + 3] == ["PATH", "/usr/bin:/bin"]')

    def test_command_goes_after_separator(self) -> None:
        argv = build_zygote_argv(_profile(), ["echo", "hi"], env={})
        sep = argv.index("--")
        if argv[sep + 1 :] != ["echo", "hi"]:
            raise AssertionError('argv[sep + 1 :] == ["echo", "hi"]')

    def test_process_limit_stays_out_of_argv(self) -> None:
        """RLIMIT_NPROC ставит себе сама зигота: в argv bwrap его нет."""
        argv = build_zygote_argv(_profile(max_processes=64), ["echo", "hi"], env={})
        if "ulimit" in " ".join(argv):
            raise AssertionError('"ulimit" not in " ".join(argv)')


@pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="требуется bubblewrap (`apt install bubblewrap`)",
)
class TestBashTool:
    """Интеграционные: реально запускают bwrap."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    LIMITS: ClassVar[BashToolConfig] = BashToolConfig(max_output_bytes=4 * 1024 * 1024)

    @classmethod
    def _make_tool(
        cls,
        workspace_root: Path,
        profile: SandboxProfile | None = None,
        limits: BashToolConfig | None = None,
    ):
        ws = str(workspace_root)
        base = profile or _profile()
        ro_binds = (*_HOST_RO_BINDS, *_PYTHON_BINDS)
        parsed: list[BindSpec] = []
        for item in ro_binds:
            parsed.append(BindSpec.parse(item))

        mounts = base.mounts.model_copy(
            update={"ro": tuple(parsed), "rw": (BindSpec.parse(ws),)}
        )
        isolation = base.isolation.model_copy(update={"env": dict(_SANDBOX_ENV)})
        run = base.run.model_copy(update={"cwd": ws})
        profile_dto = base.model_copy(
            update={"mounts": mounts, "isolation": isolation, "run": run}
        )
        sandbox = SandboxToolConfig(profile=profile_dto)
        profile = sandbox.profile

        output = limits
        if output is None:
            output = cls.LIMITS

        tmp_size = profile.mounts.tmpfs[0].size_bytes
        section = f"bash-{workspace_root.name}-{profile.limits.timeout_sec}-{tmp_size}"
        supervisor = ZygoteRegistry.obtain(section, profile, (), _ZYGOTE)
        caller = ZygoteToolCaller(section, supervisor, profile)

        return as_structured_tool(build_bash_tool(output, lambda tool: caller))

    @staticmethod
    def _invoke(tool, **args) -> ShellResult:
        args.setdefault("stdin", "")
        msg: ToolMessage = tool.invoke(_tool_call("bash", args))
        if not (isinstance(msg.artifact, ShellResult)):
            raise AssertionError("isinstance(msg.artifact, ShellResult)")
        return msg.artifact

    def test_echo_inside_sandbox(self, tmp_path: Path) -> None:
        payload = self._invoke(self._make_tool(tmp_path), command="echo hello")
        if payload.exit_code != 0:
            raise AssertionError("payload.exit_code == 0")
        if payload.stdout.rstrip() != "hello":
            raise AssertionError('payload.stdout.rstrip() == "hello"')
        if payload.timed_out:
            raise AssertionError("not payload.timed_out")

    def test_cwd_is_workspace_root(self, tmp_path: Path) -> None:
        payload = self._invoke(self._make_tool(tmp_path), command="pwd")
        if payload.stdout.rstrip() != str(tmp_path.resolve()):
            raise AssertionError("payload.stdout.rstrip() == str(tmp_path.resolve(…")

    def test_workspace_writes_persist_on_host(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo content > out.txt",
        )
        if payload.exit_code != 0:
            raise AssertionError("payload.exit_code == 0")
        if (tmp_path / "out.txt").read_text() != "content\n":
            raise AssertionError('(tmp_path / "out.txt").read_text() == "content\\n"')

    def test_outside_workspace_write_does_not_reach_host(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo x > /etc/from-sandbox 2>&1; echo rc=$?",
        )
        if payload.exit_code != 0:
            raise AssertionError("payload.exit_code == 0")
        if Path("/etc/from-sandbox").exists():
            raise AssertionError('not Path("/etc/from-sandbox").exists()')

    def test_ro_bind_write_denied(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo x > /usr/from-sandbox 2>&1",
        )
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")
        if Path("/usr/from-sandbox").exists():
            raise AssertionError('not Path("/usr/from-sandbox").exists()')

    def test_network_disabled_by_default(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="getent hosts example.com 2>&1; echo done-$?",
        )
        if not ("done-2" in payload.stdout or "done-1" in payload.stdout):
            raise AssertionError('"done-2" in payload.stdout or "done-1" in payloa…')

    def test_timeout_marks_timed_out(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path, _profile(timeout_sec=1)),
            command="sleep 10",
        )
        if not (payload.timed_out):
            raise AssertionError("payload.timed_out")

    def test_llm_does_not_choose_profile(self, tmp_path: Path) -> None:
        """Профиль задаёт конфиг: у инструмента нет такого аргумента."""
        tool = self._make_tool(tmp_path)
        schema = cast(type[BaseModel], tool.args_schema)
        if set(schema.model_fields) != {"command", "stdin"}:
            raise AssertionError('set(schema.model_fields) == {"command", "stdin"}')

    def test_pid_namespace_isolation(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="ps -e --no-headers | wc -l",
        )
        if int(payload.stdout.strip()) >= 10:
            raise AssertionError("int(payload.stdout.strip()) < 10")

    def test_memory_limit_applied_without_image(self, tmp_path: Path) -> None:
        tool = self._make_tool(
            tmp_path, _profile(process_memory_bytes=64 * 1024 * 1024)
        )
        payload = self._invoke(tool, command="ulimit -v")
        if payload.stdout.strip() != str(64 * 1024):
            raise AssertionError("payload.stdout.strip() == str(64 * 1024)")

    def test_cpu_limit_applied_without_image(self, tmp_path: Path) -> None:
        tool = self._make_tool(tmp_path, _profile(process_cpu_sec=5))
        payload = self._invoke(tool, command="ulimit -t")
        if payload.stdout.strip() != "5":
            raise AssertionError('payload.stdout.strip() == "5"')

    def test_tmpfs_size_limit_enforced(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path, _profile(tmpfs=("/tmp:1M",))),  # noqa: S108
            command="dd if=/dev/zero of=/tmp/blob bs=1M count=4 2>&1; echo rc=$?",
        )
        if "rc=0" in payload.stdout:
            raise AssertionError('"rc=0" not in payload.stdout')

    def test_per_call_variables_in_binds_are_refused(self, tmp_path: Path) -> None:
        """Бинды зиготы статичны: пути на вызов живут только в rw_images."""
        template = f"{tmp_path}/{{user_id}}/{{thread_id}}"
        profile_dto = _profile(
            ro=(*_HOST_RO_BINDS, *_PYTHON_BINDS),
            rw=(template,),
            env=dict(_SANDBOX_ENV),
            cwd=template,
        )
        profile = SandboxToolConfig(profile=profile_dto).profile

        with pytest.raises(LauncherError, match="per-call path variables"):
            ZygoteRegistry.obtain("bash-vars", profile, (), _ZYGOTE)

    def test_short_output_is_not_clipped(self, tmp_path: Path) -> None:
        payload = self._invoke(self._make_tool(tmp_path), command="echo hello")
        if payload.stdout_truncated:
            raise AssertionError("not payload.stdout_truncated")
        if payload.stdout_bytes != len(b"hello\n"):
            raise AssertionError('payload.stdout_bytes == len(b"hello\\n")')

    def test_large_output_is_clipped_to_budget(self, tmp_path: Path) -> None:
        limits = BashToolConfig(max_output_bytes=200)
        tool = self._make_tool(tmp_path, limits=limits)

        payload = self._invoke(tool, command="seq 1 100000")

        if payload.exit_code != 0:
            raise AssertionError("payload.exit_code == 0")
        if not (payload.stdout_truncated):
            raise AssertionError("payload.stdout_truncated")
        if payload.stdout_bytes <= 500_000:
            raise AssertionError("payload.stdout_bytes > 500_000")
        if not (payload.stdout.startswith("1\n2\n3\n")):
            raise AssertionError('payload.stdout.startswith("1\\n2\\n3\\n")')
        if "truncated: 200 of" not in payload.stdout:
            raise AssertionError('"truncated: 200 of" in payload.stdout')
