"""Backward-compat launcher for the existing DSH MCP connection.

The installed package is the real thing (`solidworks-mcp`); this file just
forwards to it so the DSH connection registered with this absolute path keeps
working after the packaging refactor.
"""

import os
import sys

if __name__ == "__main__":
    try:
        from solidworks_mcp.server import main  # installed package
    except ImportError:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
        from solidworks_mcp.server import main
    sys.exit(main())
