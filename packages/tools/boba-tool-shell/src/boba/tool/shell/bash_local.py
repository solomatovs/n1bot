"""`bash_local`: запуск shell-команды без bubblewrap-изоляции.

В отличие от `bash_sandbox`, эта реализация запускает процесс напрямую
через `subprocess.Popen` — без namespace'ов, без mount-биндов, с тем же
доступом к ФС/сети, что и у самого агента. Использовать только если
хост-машине вы доверяете коду от LLM (например, dev-окружение, где
bubblewrap недоступен).

Безопасность Python-стороны: `command` от LLM передаётся как единичный
argv-элемент в `bash -c`, без shell-интерполяции на стороне Python.
Шелл-интерпретация `command` — полная, под привилегиями агента, поэтому
tool опасен и должен быть включён только осознанно.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import Field

from boba.tool.shell._profile_local import resolve_local_env
from boba.tool.shell._runner import RunResult, run_subprocess
from boba.tool.shell.config import BashLocalConfig
from boba.tools import FromConfig, tool

__all__ = ["bash_local"]


_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024  # 1 MiB
_BASH_BIN = "/bin/bash"


@tool
def bash_local(
    command: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_MAX_COMMAND_LEN,
            description="Shell-команда (передаётся в `bash -c`).",
        ),
    ],
    cfg: Annotated[BashLocalConfig, FromConfig()],
    stdin: Annotated[
        str,
        Field(
            max_length=_MAX_STDIN_LEN,
            description=(
                "Stdin для команды (UTF-8). Пустая строка = нет stdin."
            ),
        ),
    ] = "",
) -> dict[str, Any]:
    """Выполнить shell-команду через `bash -c` без изоляции.

    Запускается напрямую под пользователем агента; доступ к ФС и сети —
    полный, как у самого процесса агента. Используйте `bash_sandbox`
    для изоляции.
    """
    argv = [_BASH_BIN, "-c", command]
    cwd = cfg.cwd or str(cfg.workspace_root)
    env = resolve_local_env(cfg.env_passthrough, cfg.env_set, os.environ)
    result = run_subprocess(
        argv,
        stdin_data=stdin.encode("utf-8"),
        timeout_sec=cfg.timeout_sec,
        max_output_bytes=cfg.max_output_bytes,
        cwd=cwd,
        env=env,
    )
    return _result_to_payload(result)


def _result_to_payload(result: RunResult) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated_stdout": result.truncated_stdout,
        "truncated_stderr": result.truncated_stderr,
        "timed_out": result.timed_out,
    }
