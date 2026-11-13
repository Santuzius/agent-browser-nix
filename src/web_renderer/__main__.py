"""Entry point of the `web-renderer` command: `web-renderer mcp` runs the MCP server over stdio, anything else is the command line (`web-renderer --help`)."""

import asyncio
import sys

from .environment import ensure_playwright


def main():
    ensure_playwright()
    if sys.argv[1:2] == ["mcp"]:
        from .mcp_server import serve
        asyncio.run(serve())
    else:
        from .cli import main as cli_main
        cli_main()


main()
