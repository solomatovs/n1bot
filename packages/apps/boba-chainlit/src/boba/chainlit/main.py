"""Точка входа: собирает приложение и отдаёт его ASGI-серверу."""

from boba.chainlit.infra.entry import AppEntry

if __name__ == "__main__":
    AppEntry.run()
