"""python -m boba.mcp_server serve|health --config <toml> [--config <наложение>]
и служебные подкоманды launcher, probe, self-check (boba.mcp_server.program)."""

from boba.mcp_server.program import cli

if __name__ == "__main__":
    cli()
