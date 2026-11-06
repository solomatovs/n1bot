"""Изолированный процесс сетевого стенда reload: PostgreSQL, OAuth и MCP."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import sys
from pathlib import Path

import uvicorn
from service_stand import ServiceStand

from boba.cancellation import ProcessStop
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.http_server import SignalledServer
from boba.krb import KerberosWorkspace
from boba.krb.layout import InstanceConf
from boba.mcp_server.app import EndedSubscriptions, StoppedRuns
from boba.mcp_server.endpoints import McpEndpointStore
from boba.mcp_server.reload import ReloadableMcp
from boba.mcp_server.server import RunLimits
from boba.stand.site import StandConfigEnv, StandConfigPaths


class ReloadProcess:
    """Два унаследованных listener'а одного процесса с production-обработчиками."""

    def __init__(self, workdir: Path, dbname: str, schema: str, fds: list[int]) -> None:
        self._listeners = [socket.socket(fileno=fd) for fd in fds]
        paths = StandConfigPaths(StandConfigEnv.SERVICE)
        raw = paths.load()
        kerberos = paths.main().parent / InstanceConf.KRB5_CONF.value
        KerberosWorkspace.configure(str(kerberos), str(workdir / "krb-cache"))
        postgres = bind(raw, "postgres", PostgresConfig)
        self._pool = AsyncPostgresPool(postgres.model_copy(update={"dbname": dbname}))
        self._stand = ServiceStand(
            workdir,
            RunLimits(max_runs=2, max_waiting=0),
            self._listeners[0].getsockname()[1],
            "",
            dynamic=True,
        )
        self._reload = ReloadableMcp(
            McpEndpointStore(self._pool, schema), self._stand.catalog, self._stand.build
        )
        self._stop = ProcessStop()
        self._stop.on_stop(StoppedRuns(self._stand.runs))
        self._stop.on_stop(EndedSubscriptions(self._reload))
        self._stop.signals.on_async(signal.SIGHUP, self._reload.on_reload)
        self._server = SignalledServer(
            uvicorn.Config(
                self._reload.app(), log_config=None, timeout_graceful_shutdown=3
            ),
            self._stop,
        )

    async def run(self) -> None:
        await self._pool.open()
        try:
            await self._server.serve_until_stopped(self._listeners)
        finally:
            await self._pool.close()
            for listener in self._listeners:
                listener.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    descriptors = [int(value) for value in sys.argv[4:]]
    process = ReloadProcess(Path(sys.argv[1]), sys.argv[2], sys.argv[3], descriptors)
    await process.run()


if __name__ == "__main__":
    asyncio.run(main())
