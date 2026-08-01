"""Хелперы bash_local: env для subprocess из allowlist + overrides."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = ["DEFAULT_PASSTHROUGH", "resolve_local_env"]


# минимум, чтобы работали базовые команды и не ломалась локаль
DEFAULT_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
)


def resolve_local_env(
    env_passthrough: Iterable[str],
    env_set: Mapping[str, str],
    host_env: Mapping[str, str],
) -> dict[str, str]:
    """env для Popen: host_env по allowlist, сверху env_set."""
    result = {
        name: host_env[name]
        for name in env_passthrough
        if name in host_env
    }
    result.update(env_set)
    return result
