"""Console entry point: `solidworks-mcp` (serve) or `solidworks-mcp --selftest`."""

import sys


def main() -> int:
    if "--selftest" in sys.argv:
        from solidworks_mcp.server import selftest
        return selftest()
    from solidworks_mcp.server import build_server
    build_server().run()   # stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
