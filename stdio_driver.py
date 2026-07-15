"""Live-stdio driver for shell-0.

Launches server.py as a subprocess and speaks MCP over stdio, so both the
integration tests and manual.py drive the *real* server exactly as an MCP client
would. Thin wrapper over the official `mcp` SDK client.
"""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent
SERVER_PY = REPO_ROOT / "server.py"


@asynccontextmanager
async def connect(env: Optional[dict] = None, cwd: Optional[str] = None):
    """Yield an initialized MCP ClientSession connected to a fresh shell-0.

    env:  extra environment variables for the server process (merged over a copy
          of the current environment - e.g. FS_AUDIT_ROOT to redirect audit).
    cwd:  working directory for the server process (defaults to the repo root).
    """
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    if env:
        child_env.update(env)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PY)],
        env=child_env,
        cwd=cwd or str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # Guard the handshake: a server that starts but stalls before serving
            # would otherwise hang the caller (and the whole suite) forever.
            await asyncio.wait_for(session.initialize(), timeout=30)
            yield session


def parse(result: Any) -> Any:
    """Extract shell-0's JSON payload from an MCP CallToolResult.

    Tools return a single TextContent whose text is a JSON object; fall back to
    raw text if it is not JSON.
    """
    content = getattr(result, "content", None) or []
    if not content:
        return {"_empty": True}
    text = getattr(content[0], "text", None)
    if text is None:
        return {"_nontext": repr(content[0])}
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"_raw": text}


async def call(session: ClientSession, name: str, arguments: dict) -> Any:
    """Call a tool and return its parsed JSON payload."""
    result = await session.call_tool(name, arguments)
    return parse(result)
