"""End-to-end smoke tests: drive the real shell-0 server over live MCP stdio.

These spawn `server.py` as a subprocess and speak MCP as a client, so they
exercise the wiring the in-process tests skip: schema registration, dispatch,
output truncation, surrogate sanitization, and JSON-RPC framing.
"""
import json

from stdio_driver import call, connect


def _audit_env(tmp_path):
    return {
        "FS_AUDIT_ROOT": str(tmp_path / "fsaud"),
        "EXEC_AUDIT_ROOT": str(tmp_path / "execaud"),
    }


async def test_lists_four_tools(tmp_path):
    async with connect(env=_audit_env(tmp_path), cwd=str(tmp_path)) as session:
        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
    assert names == ["fs", "js_exec", "python_exec", "terminal"]


async def test_tool_schemas_valid(tmp_path):
    async with connect(env=_audit_env(tmp_path), cwd=str(tmp_path)) as session:
        tools = await session.list_tools()
        for t in tools.tools:
            assert t.description, f"{t.name} missing description"
            schema = t.inputSchema
            assert schema.get("type") == "object"
            assert "properties" in schema


async def test_fs_roundtrip_over_stdio(tmp_path):
    target = tmp_path / "hello.txt"
    async with connect(env=_audit_env(tmp_path), cwd=str(tmp_path)) as session:
        w = await call(session, "fs", {"action": "write", "path": str(target), "content": "over-stdio\n"})
        assert w["success"] is True
        r = await call(session, "fs", {"action": "read", "path": str(target)})
    assert r["content"] == "over-stdio\n"


async def test_python_exec_over_stdio(tmp_path):
    async with connect(env=_audit_env(tmp_path), cwd=str(tmp_path)) as session:
        r = await call(session, "python_exec", {"code": "print('via mcp'); 6 * 7"})
    assert r["success"] is True
    assert r["result"] == 42
    assert "via mcp" in r["stdout"]


async def test_terminal_over_stdio(tmp_path):
    async with connect(env=_audit_env(tmp_path), cwd=str(tmp_path)) as session:
        r = await call(session, "terminal", {"command": "echo mcp-echo"})
    assert r["success"] is True
    assert "mcp-echo" in r["output"]


async def test_unknown_tool_is_rejected(tmp_path):
    async with connect(env=_audit_env(tmp_path), cwd=str(tmp_path)) as session:
        try:
            result = await session.call_tool("does_not_exist", {})
        except Exception:
            return  # SDK-level rejection is acceptable
        parsed = json.loads(result.content[0].text) if result.content else {}
        assert parsed.get("success") is False or getattr(result, "isError", False)


async def test_output_truncation_over_stdio(tmp_path):
    # The server caps serialized output (OUTPUT_MAX_CHARS) with a valid truncated
    # envelope - oversized output must not break JSON-RPC framing.
    env = {**_audit_env(tmp_path), "OUTPUT_MAX_CHARS": "2000"}
    async with connect(env=env, cwd=str(tmp_path)) as session:
        r = await call(session, "python_exec", {"code": "print('X' * 50000)"})
    # parse() returns a dict from real JSON; a framing break would have yielded
    # a {"_raw": ...} fallback or an exception.
    assert isinstance(r, dict)
    assert "_raw" not in r
    # ...and it must have ACTUALLY truncated (fallback envelope or field-trim),
    # otherwise this proves nothing.
    assert r.get("truncated") is True or r.get("_truncated_field")
