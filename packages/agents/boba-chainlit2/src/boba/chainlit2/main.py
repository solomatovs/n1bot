"""Точка входа: собирает приложение и отдаёт его ASGI-серверу."""

from boba.chainlit2.infra.bootstrap import run_app

if __name__ == "__main__":
    run_app()
