"""Coverage + regression tests for js_exec (tools/js_runner.py).

Skipped entirely when Node.js is not on PATH. Uses the `workdir` fixture because
js_exec writes its persistent state file and temp scripts into os.getcwd().
"""
import asyncio
import shutil

import pytest

import tools.js_runner as jsmod

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not on PATH")


async def test_console_output(workdir):
    r = await jsmod.exec_js("console.log('hello from node')")
    assert r["success"] is True
    assert "hello from node" in r["output"]


async def test_error_on_throw(workdir):
    r = await jsmod.exec_js("throw new Error('boom')")
    assert r["success"] is False
    assert "boom" in (r["output"] + r.get("stderr", ""))
    assert r.get("exitCode", 0) != 0


async def test_persistent_state(workdir):
    r1 = await jsmod.exec_js("state.count = (state.count || 0) + 1; console.log(state.count)")
    assert r1["success"] is True
    assert "1" in r1["output"]
    r2 = await jsmod.exec_js("state.count = (state.count || 0) + 1; console.log(state.count)")
    assert "2" in r2["output"]


@pytest.mark.regression
async def test_concurrent_state_no_corruption(workdir):
    # REGRESSION: concurrent exec_js calls once raced the shared on-disk state
    # file; an asyncio.Lock must serialize them so no update is lost.
    # (session 2026-07-13)
    async def bump(i):
        return await jsmod.exec_js(f"state['k{i}'] = {i}; console.log('done{i}')")

    results = await asyncio.gather(*[bump(i) for i in range(6)])
    assert all(r["success"] for r in results)
    final = await jsmod.exec_js("console.log(JSON.stringify(Object.keys(state).sort()))")
    assert final["success"] is True
    for i in range(6):
        assert f"k{i}" in final["output"]


async def test_autofix_strips_fence(workdir):
    r = await jsmod.exec_js("```js\nconsole.log('fenced')\n```")
    assert r["success"] is True
    assert "fenced" in r["output"]


@pytest.mark.regression
async def test_audit_written_on_success_and_error(workdir, audit_on):
    # REGRESSION: js_exec once had ZERO audit logging; success -> _js_ok.js and
    # failure -> _js_err.js must now be written. (session 2026-07-13)
    exec_audit = audit_on["exec_audit"]
    await jsmod.exec_js("console.log('good')")
    await jsmod.exec_js("throw new Error('bad')")
    names = [f.name for f in exec_audit.rglob("*_js_*.js")]
    assert any(n.endswith("_js_ok.js") for n in names), names
    assert any(n.endswith("_js_err.js") for n in names), names
