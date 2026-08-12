"""Тесты инструментов: bash в bwrap-песочнице и сборка её argv."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import cast

import pytest
from bash_stage import BashStageSetup
from chainlit.context import init_http_context
from chainlit.user import PersistedUser
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.chainlit.agent.tools.run_log import ToolRunLogger
from boba.sandbox.argv import ChannelArgv, WrapArgsCodec
from boba.sandbox.profile import BindSpec, SandboxProfile, SandboxToolConfig
from boba.stand.journal import CallStand
from boba.tool.shell import BashStage
from boba.toolkit.channels import Channel, StreamKey
from boba.toolkit.result import JsonResult


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _tool_call(name: str, args: dict) -> dict:
    return {"args": args, "id": f"call-{name}", "name": name, "type": "tool_call"}


_PROFILE_BASE: dict[str, object] = {
    "rootfs": "",
    "ro_binds": (),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "lock_wait_sec": 10.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "tmpfs": (),
    "network": False,
    "env_set": {},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 256,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "",
}


def _profile(**kw: object) -> SandboxProfile:
    """Все поля профиля обязательны; база даёт валидный минимум для тестов."""
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


_HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")


class TestProfileValidation:
    def test_zero_memory_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_memory_bytes"):
            _profile(max_memory_bytes=0)

    def test_zero_cpu_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_cpu_sec"):
            _profile(max_cpu_sec=0)

    def test_zero_file_size_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_file_size_bytes"):
            _profile(max_file_size_bytes=0)

    def test_zero_open_files_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_open_files"):
            _profile(max_open_files=0)

    def test_zero_process_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_processes"):
            _profile(max_processes=0)

    def test_all_fields_required(self) -> None:
        with pytest.raises(ValueError, match="Field required"):
            SandboxProfile.model_validate({})


class TestBwrapArgv:
    """Юниты pure-builder'а argv: не требуют установленного bwrap.

    Профиль едет каналом wrap_args, поэтому проверяются его опции, а не argv.
    """

    _WS = "/srv/workspace"
    _WRAP_ARGS_FD = 3
    _REDIRECT_PREFIX = "exec >&4 2>&5"

    @classmethod
    def _built(
        cls,
        profile: SandboxProfile,
        command: str,
        env: dict[str, str],
    ) -> ChannelArgv:
        return ChannelArgv.build(
            profile,
            command,
            env=env,
            wrap_args_fd=cls._WRAP_ARGS_FD,
            redirect_prefix=cls._REDIRECT_PREFIX,
        )

    @classmethod
    def _options(
        cls,
        profile: SandboxProfile,
        command: str = "true",
        env: dict[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Опции профиля так, как их прочтёт bwrap из канала wrap_args."""
        if env is None:
            env = {}

        return WrapArgsCodec.decode(cls._built(profile, command, env).wrap_args)

    def test_argv_carries_only_args_fd_and_command(self) -> None:
        argv = self._built(_profile(), "echo hi", {}).argv
        assert argv[0].endswith("bwrap")
        assert argv[1:3] == ("--args", str(self._WRAP_ARGS_FD))

    def test_isolation_flags_travel_in_wrap_args(self) -> None:
        options = self._options(_profile(), "echo hi")
        assert "--die-with-parent" in options
        assert "--unshare-user" in options
        assert "--unshare-pid" in options
        assert "--new-session" in options

    def test_userns_creation_disabled(self) -> None:
        assert "--disable-userns" in self._options(_profile())

    def test_neutral_hostname(self) -> None:
        options = self._options(_profile())
        assert options[options.index("--hostname") + 1] == "sandbox"

    def test_network_disabled_adds_unshare_net(self) -> None:
        assert "--unshare-net" in self._options(_profile(network=False))

    def test_network_enabled_omits_unshare_net(self) -> None:
        assert "--unshare-net" not in self._options(_profile(network=True))

    def test_no_implicit_rw_binds(self) -> None:
        options = self._options(_profile())
        assert "--bind-try" not in options
        assert "--bind" not in options

    def test_rw_bind_same_path_and_chdir(self) -> None:
        profile = _profile(rw_binds=(self._WS,), cwd=self._WS)
        options = self._options(profile)
        index = options.index("--bind-try")
        assert options[index + 1 : index + 3] == (self._WS, self._WS)
        assert options[options.index("--chdir") + 1] == self._WS

    def test_rw_bind_with_explicit_target(self) -> None:
        profile = _profile(rw_binds=(f"{self._WS}:/workspace",), cwd="/workspace")
        options = self._options(profile)
        index = options.index("--bind-try")
        assert options[index + 1 : index + 3] == (self._WS, "/workspace")
        assert options[options.index("--chdir") + 1] == "/workspace"

    def test_empty_cwd_means_root(self) -> None:
        options = self._options(_profile())
        assert options[options.index("--chdir") + 1] == "/"

    def test_tmpfs_without_size_rejected(self) -> None:
        """Размер обязателен: неявного «без лимита» больше нет."""
        with pytest.raises(ValueError, match="size is required"):
            _profile(tmpfs=("/tmp",))  # noqa: S108

    def test_tmpfs_size_precedes_mount(self) -> None:
        options = self._options(_profile(tmpfs=("/tmp:64M",)))  # noqa: S108
        index = options.index("--size")
        assert options[index + 1] == str(64 * 1024**2)
        assert options[index + 2 : index + 4] == ("--tmpfs", "/tmp")  # noqa: S108

    def test_tmpfs_bad_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid size"):
            _profile(tmpfs=("/tmp:64X",))  # noqa: S108

    def test_rootfs_mounted_as_root_before_proc_dev(self) -> None:
        options = self._options(_profile(rootfs="/srv/rootfs", ro_binds=()))
        index = options.index("--ro-bind")
        assert options[index + 1 : index + 3] == ("/srv/rootfs", "/")
        assert index < options.index("--proc")
        assert index < options.index("--dev")

    def test_env_cleared_and_set(self) -> None:
        options = self._options(_profile(), env={"PATH": "/usr/bin:/bin"})
        assert "--clearenv" in options
        index = options.index("--setenv")
        assert options[index + 1 : index + 3] == ("PATH", "/usr/bin:/bin")

    def test_command_goes_after_separator(self) -> None:
        argv = self._built(_profile(), "echo hi", {}).argv
        sep = argv.index("--")
        assert argv[sep + 1 : sep + 3] == ("/bin/bash", "-c")
        assert argv[sep + 3].endswith("; echo hi")

    def test_process_limit_prefixes_command(self) -> None:
        argv = self._built(_profile(max_processes=64), "echo hi", {}).argv
        expected = f"ulimit -u 64 || exit 1; {self._REDIRECT_PREFIX}; echo hi"
        assert argv[-1] == expected


@pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="требуется bubblewrap (`apt install bubblewrap`)",
)
class TestBashTool:
    """Интеграционные: реально запускают bwrap."""

    @staticmethod
    def _make_tool(workspace_root: Path, profile: SandboxProfile | None = None):
        ws = str(workspace_root)
        base = profile or _profile()
        profile_dto = base.model_copy(
            update={
                "ro_binds": tuple(BindSpec.parse(p) for p in _HOST_RO_BINDS),
                "rw_binds": (BindSpec.parse(ws),),
                "cwd": ws,
            },
        )
        sandbox = SandboxToolConfig(profile=profile_dto, override={})
        profile = sandbox.effective()
        return BashStageSetup.tool(profile, dict)

    @staticmethod
    def _invoke(tool, **args) -> dict:
        args.setdefault("stdin", "")
        msg: ToolMessage = tool.invoke(_tool_call("bash", args))
        assert isinstance(msg.artifact, JsonResult)
        return msg.artifact.payload

    def test_echo_inside_sandbox(self, tmp_path: Path) -> None:
        payload = self._invoke(self._make_tool(tmp_path), command="echo hello")
        assert payload["exit_code"] == 0
        assert payload["stdout"].rstrip() == "hello"
        assert not payload["timed_out"]

    def test_cwd_is_workspace_root(self, tmp_path: Path) -> None:
        payload = self._invoke(self._make_tool(tmp_path), command="pwd")
        assert payload["stdout"].rstrip() == str(tmp_path.resolve())

    def test_workspace_writes_persist_on_host(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo content > out.txt",
        )
        assert payload["exit_code"] == 0
        assert (tmp_path / "out.txt").read_text() == "content\n"

    def test_outside_workspace_write_does_not_reach_host(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo x > /etc/from-sandbox 2>&1; echo rc=$?",
        )
        assert payload["exit_code"] == 0
        assert not Path("/etc/from-sandbox").exists()

    def test_ro_bind_write_denied(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo x > /usr/from-sandbox 2>&1; echo rc=$?",
        )
        assert "rc=0" not in payload["stdout"]
        assert not Path("/usr/from-sandbox").exists()

    def test_network_disabled_by_default(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="getent hosts example.com 2>&1; echo done-$?",
        )
        assert "done-2" in payload["stdout"] or "done-1" in payload["stdout"]

    def test_timeout_marks_timed_out(self, tmp_path: Path) -> None:
        """Стадия по дедлайну — отказ: причина едет текстом раннера."""
        payload = self._invoke(
            self._make_tool(tmp_path, _profile(timeout_sec=1)),
            command="sleep 10",
        )
        assert "timed out" in payload["message"]

    def test_llm_does_not_choose_profile(self, tmp_path: Path) -> None:
        """Профиль задаёт конфиг: у инструмента нет такого аргумента."""
        tool = self._make_tool(tmp_path)
        schema = cast(type[BaseModel], tool.args_schema)
        assert set(schema.model_fields) == {"command", "stdin"}

    def test_pid_namespace_isolation(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="ps -e --no-headers | wc -l",
        )
        assert int(payload["stdout"].strip()) < 10

    def test_memory_limit_applied_without_image(self, tmp_path: Path) -> None:
        # обвязка узла — python: 64 МиБ адресного пространства ей мало на импорты
        limit_bytes = 512 * 1024 * 1024
        tool = self._make_tool(tmp_path, _profile(max_memory_bytes=limit_bytes))
        payload = self._invoke(tool, command="ulimit -v")
        assert payload["stdout"].strip() == str(limit_bytes // 1024)

    def test_cpu_limit_applied_without_image(self, tmp_path: Path) -> None:
        tool = self._make_tool(tmp_path, _profile(max_cpu_sec=5))
        payload = self._invoke(tool, command="ulimit -t")
        assert payload["stdout"].strip() == "5"

    def test_tmpfs_size_limit_enforced(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path, _profile(tmpfs=("/tmp:1M",))),  # noqa: S108
            command="dd if=/dev/zero of=/tmp/blob bs=1M count=4 2>&1; echo rc=$?",
        )
        assert "rc=0" not in payload["stdout"]

    def test_placeholders_render_and_dirs_created(self, tmp_path: Path) -> None:
        template = f"{tmp_path}/{{user_id}}/{{thread_id}}"
        profile_dto = _profile(
            ro_binds=_HOST_RO_BINDS,
            rw_binds=(template,),
            cwd=template,
        )
        profile = SandboxToolConfig(profile=profile_dto, override={}).effective()
        tool = BashStageSetup.tool(
            profile, lambda: {"user_id": "7", "thread_id": "t1"}
        )
        payload = self._invoke(tool, command="echo data > out.txt")
        assert payload["exit_code"] == 0
        assert (tmp_path / "7" / "t1" / "out.txt").read_text() == "data\n"


@pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="требуется bubblewrap (`apt install bubblewrap`)",
)
class TestBashJournalAddress:
    """Адрес журнала собирается из сессии и id вызова langchain.

    Стык обвязки и песочницы: id вызова доезжает до ToolCallContext полем
    InjectedToolCallId, а песочница называет им файлы каналов.
    """

    USER_ID = "7"
    THREAD_ID = "th-journal-address"
    CALL_ID = "call-bash"
    """id из _tool_call: langchain отдаёт его инструменту вместе с аргументами."""

    def _in_session(self, tool: BaseTool, command: str) -> None:
        async def scenario() -> None:
            user = PersistedUser(
                id=self.USER_ID,
                identifier="tester",
                createdAt="2026-01-01T00:00:00Z",
            )
            init_http_context(user=user, thread_id=self.THREAD_ID)
            tool.invoke(_tool_call("bash", {"command": command, "stdin": ""}))

        asyncio.run(scenario())

    def _key(self, channel: Channel) -> StreamKey:
        return StreamKey(
            user_id=self.USER_ID,
            thread_id=self.THREAD_ID,
            call_id=self.CALL_ID,
            stage=BashStage.NAME,
            channel=channel,
        )

    def test_the_call_id_of_the_llm_names_the_files(self, tmp_path: Path) -> None:
        tool = TestBashTool._make_tool(tmp_path)
        ToolRunLogger.guard_all([tool])

        self._in_session(tool, "echo привет; echo шум >&2")

        journal = CallStand.journal()

        payload = journal.slice_at(self._key(Channel.TOOL_PAYLOAD), 0)
        assert payload is not None
        assert payload.text == "привет\n"
        assert payload.closed is True

        stderr = journal.slice_at(self._key(Channel.TOOL_STDERR), 0)
        assert stderr is not None
        assert "шум" in stderr.text

        root = Path(journal.vault_root(self.USER_ID)) / self.THREAD_ID
        assert (root / f"{self.CALL_ID}.{BashStage.NAME}.tool_payload.log").exists()
