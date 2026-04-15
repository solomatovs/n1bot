"""Точка входа: python -m boba.app"""

from __future__ import annotations

import logging

from boba.app.logging import configure_logging
from boba.domain.core.file_storage import FileStorage
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope

logger = logging.getLogger(__name__)


def main() -> None:
    config = ConfigLoader().load()
    configure_logging(config.log_level)

    logger.info("starting boba")

    container = create_container(config)

    # Request 1: новый workspace
    with request_scope(container) as request:
        svc1 = request.get(FileStorage)
        ws_id = svc1.workspace_id._name
        logger.info("request 1: created workspace %s", ws_id)

    # Request 2: тот же workspace — тот же экземпляр сервиса
    with request_scope(container, ws_id) as request:
        svc2 = request.get(FileStorage)
        logger.info(
            "request 2: workspace %s, same instance: %s",
            svc2.workspace_id._name,
            svc1 is svc2,
        )

    # Request 3: новая сессия — новый экземпляр
    with request_scope(container) as request:
        svc3 = request.get(FileStorage)
        logger.info(
            "request 3: workspace %s, same instance: %s",
            svc3.workspace_id._name,
            svc1 is svc3,
        )


if __name__ == "__main__":
    main()
