"""Хелперы для local-варианта shell-tool'а (без bubblewrap).

Порт boba.tool.shell._profile_local. resolve_local_env — чистая функция,
собирающая финальный env для subprocess.Popen из allowlist+overrides.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = ["DEFAULT_PASSTHROUGH", "resolve_local_env"]


# Минимально-разумный набор host-env, который наследуется по умолчанию.
# Подобран так, чтобы базовые shell-команды работали (PATH/HOME/USER) и
# чтобы локаль не ломалась в выводе утилит (LANG/LC_ALL/TERM).
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
    """Финальное env для subprocess.Popen.

    Берёт переменные из host_env по allowlist env_passthrough и накладывает
    env_set поверх (env_set перекрывает passthrough при совпадении имён).

    host_env передаётся явно (обычно os.environ), чтобы функция оставалась
    чистой и тестируемой без monkey-patch'а.
    """
    result = {
        name: host_env[name]
        for name in env_passthrough
        if name in host_env
    }
    result.update(env_set)
    return result
