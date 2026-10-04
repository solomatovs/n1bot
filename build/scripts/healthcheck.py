"""Проба готовности приложения для HEALTHCHECK образа.

Порт, префикс и путь пробы приходят из окружения контейнера (boba.env):
запрос идёт тем же интерпретатором, что и приложение.
"""

import os
import sys
import urllib.request
from enum import StrEnum


class HealthEnv(StrEnum):
    PORT = "BOBA_PORT"
    PREFIX = "BOBA_URL_PREFIX"
    PATH = "BOBA_HEALTH_PATH"


def main() -> int:
    port = os.environ[HealthEnv.PORT]
    prefix = os.environ[HealthEnv.PREFIX]
    path = os.environ[HealthEnv.PATH]
    url = f"http://127.0.0.1:{port}{prefix}{path}"

    # схема собрана здесь же и всегда http: проверка ради явного отказа от file:
    if not url.startswith("http://"):
        raise SystemExit(f"healthcheck: expected an http url, got {url!r}")

    with urllib.request.urlopen(url, timeout=4) as response:  # noqa: S310
        print(f"{url}: {response.status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
