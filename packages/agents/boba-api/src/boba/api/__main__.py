"""Запуск api-процесса: python -m boba.api."""

from boba.api.entry import ApiEntry

if __name__ == "__main__":
    ApiEntry.run()
