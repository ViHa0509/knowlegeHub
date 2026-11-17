"""Claude Desktop extension entry point: the kh-helper MCP server 'knowledge-hub' in desktop mode."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The extension keeps its own state, apart from a terminal kh-helper on the same computer.
os.environ.setdefault("KH_HELPER_HOME", os.path.join(os.path.expanduser("~"), ".kh-desktop", "kh-helper"))

from kh_helper import mcp_server  # noqa: E402

if __name__ == "__main__":
    mcp_server.run(os.environ.get("KH_URL") or None, desktop=True)
