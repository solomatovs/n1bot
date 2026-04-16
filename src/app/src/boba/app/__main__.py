"""Точка входа: python -m boba.app"""

from __future__ import annotations

import logging

from boba.infra.config import ConfigLoader

logger = logging.getLogger(__name__)


def main() -> None:
    _ = ConfigLoader().load()

if __name__ == "__main__":
    main()
