"""Pure builder: SandboxProfile + command → argv для запуска bwrap.

Без I/O и без subprocess — только формирование списка аргументов.
Так builder тестируется юнит-тестами в любой среде, без bwrap на машине.

Контракт argv:
- `bwrap` со стандартными изоляциями (`--die-with-parent`, `--unshare-*`,
  свой `--proc`, `--dev`, `--new-session`);
- read-only bind'ы из профиля + RW (включая workspace);
- tmpfs-mountpoints;
- env: `--clearenv` плюс жёсткий набор `env` (caller сам резолвил
  passthrough из `os.environ` и слил с `env_set`);
- `--chdir <cwd>`;
- финальный `--`, дальше `bash -c <command>`.

Существование путей _не_ проверяется здесь — это работа фазы 5
(self-check) и обёртки в `BashTool.execute`.
"""

from __future__ import annotations

from collections.abc import Mapping

from boba.tool.shell._profile import SandboxProfile

__all__ = ["build_bwrap_argv"]

_BWRAP_BIN = "bwrap"
_BASH_BIN = "/bin/bash"


def build_bwrap_argv(
    profile: SandboxProfile,
    command: str,
    *,
    workspace_root: str,
    env: Mapping[str, str],
) -> list[str]:
    """Сформировать argv для запуска `command` в песочнице по `profile`.

    `workspace_root` — абсолютный, уже-каноничный путь к корню проекта;
    автоматически добавляется в RW-binds и, если `profile.cwd` не
    задан, становится cwd внутри песочницы.

    `env` — финальный набор переменных, который должен оказаться внутри
    песочницы. Caller отвечает за резолв `profile.env_passthrough` через
    `os.environ` и слияние с `profile.env_set`.
    """
    argv: list[str] = [
        _BWRAP_BIN,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
    ]
    if not profile.network:
        argv.append("--unshare-net")

    for path in profile.ro_binds:
        argv += ["--ro-bind-try", path, path]

    rw_paths = _dedup_preserve_order((workspace_root, *profile.rw_binds))
    for path in rw_paths:
        argv += ["--bind-try", path, path]

    for path in profile.tmpfs:
        argv += ["--tmpfs", path]

    argv += ["--clearenv"]
    for name, value in env.items():
        argv += ["--setenv", name, value]

    cwd = profile.cwd if profile.cwd is not None else workspace_root
    argv += ["--chdir", cwd]

    argv += ["--", _BASH_BIN, "-c", command]
    return argv


def _dedup_preserve_order(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return tuple(out)
