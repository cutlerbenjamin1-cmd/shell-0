"""Named regressions with no natural per-tool home: the server output-hardening
helpers, plus the flagship terminal-hang repro in its true habitat (live stdio).

Most regressions live beside the tool they guard (search the suite for
"REGRESSION:"); this file covers the server layer and the one bug that only
fully reproduces over the wire. See tests/README.md for the provenance table.
"""
import asyncio
import json

import pytest

import server
from stdio_driver import call, connect

pytestmark = pytest.mark.regression


def test_sanitize_surrogates_survives_lone_surrogate():
    # REGRESSION: a lone UTF-16 surrogate in tool output raised
    # PydanticSerializationError and killed the whole response. It must be
    # scrubbed to a valid, UTF-8-encodable payload.
    payload = {"success": True, "content": "bad\ud83d-surrogate", "nested": ["\udc00x"]}
    cleaned = server._sanitize_surrogates(payload)
    # must not raise:
    json.dumps(cleaned, ensure_ascii=False).encode("utf-8")


def test_truncate_output_keeps_valid_json_envelope():
    # REGRESSION: oversized output must be trimmed to a valid JSON envelope under
    # the cap, not blow up the caller's context or corrupt framing.
    big = {"success": True, "output": "Z" * 200_000}
    out = server._truncate_output(big, tool_name="python_exec")
    encoded = json.dumps(out, ensure_ascii=False)
    assert len(encoded) <= server.OUTPUT_MAX_CHARS
    assert out.get("success") is True


async def test_no_output_command_returns_over_stdio(tmp_path):
    # REGRESSION (the original bug in its habitat): a no-output command run
    # through the live MCP stdio server must return, not hang. This is the truest
    # repro - the child once inherited the JSON-RPC stdin over stdio.
    # (session 4c9451c7, 2025-11-30)
    env = {"FS_AUDIT_ROOT": str(tmp_path / "a"), "EXEC_AUDIT_ROOT": str(tmp_path / "b")}
    async with connect(env=env, cwd=str(tmp_path)) as session:
        r = await asyncio.wait_for(call(session, "terminal", {"command": "cd ."}), timeout=30)
    assert r["success"] is True
    assert "completed" in r["output"].lower()
