"""Хелперы для local-варианта shell-tool'а (без bubblewrap).

В отличие от sandbox-варианта здесь нет «профилей» как DTO — все
operator-controlled параметры (cwd, env_passthrough, env_set,
timeout, max_output_bytes) живут плоско в LocalToolConfig
(см. plugin.py). Этот модуль предоставляет только:

- DEFAULT_PASSTHROUGH — безопасный минимум host-env переменных,
  наследуемых внутрь дочернего процесса по умолчанию.
- resolve_local_env — чистая функция, собирающая финальный env для
  subprocess.Popen из allowlist+overrides.

Безопасность: в local-режиме процесс имеет тот же доступ к ФС/сети,
что и сам агент. Это сознательное решение оператора — для dev-окружения,
где bubblewrap недоступен. **Никогда** не вписывайте в env_passthrough
переменные с секретами (API-ключи, токены): LLM-команда сможет вытащить
их в stdout (через printenv/env) и они окажутся в JsonResult.
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

    Берёт переменные из host_env по allowlist env_passthrough и
    накладывает env_set поверх (env_set перекрывает passthrough при
    совпадении имён).

    host_env передаётся явно (обычно os.environ), чтобы функция
    оставалась чистой и тестируемой без monkey-patch'а.
    """
    result = {
        name: host_env[name]
        for name in env_passthrough
        if name in host_env
    }
    result.update(env_set)
    return result
