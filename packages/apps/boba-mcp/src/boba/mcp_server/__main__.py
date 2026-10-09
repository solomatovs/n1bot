"""python -m boba.mcp_server --config <toml> --site <toml>."""

import asyncio

from boba.mcp_server.app import main

if __name__ == "__main__":
    asyncio.run(main())
