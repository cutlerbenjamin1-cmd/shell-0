"""Coverage + regression tests for python_exec (tools/python_runner.py + worker).

The worker is a persistent subprocess; conftest resets it between tests, so state
that must persist within a test survives across calls, but never leaks across
tests.
"""
import pytest

import tools.python_runner as pymod


async def test_stdout_capture():
    r = await pymod.exec_python("print('hi there')")
    assert r["success"] is True
    assert "hi there" in r["stdout"]


async def test_last_expression_result():
    r = await pymod.exec_python("21 * 2")
    assert r["success"] is True
    assert r["result"] == 42


async def test_error_is_structured():
    r = await pymod.exec_python("1 / 0")
    assert r["success"] is False
    assert r["error"]["type"] == "ZeroDivisionError"
    assert r["error"]["traceback"]


async def test_syntax_error_reported_not_crash():
    r = await pymod.exec_python("def broken(:\n    pass")
    assert r["success"] is False
    # a following call still works -> the server did not fall over
    r2 = await pymod.exec_python("1 + 1")
    assert r2["result"] == 2


@pytest.mark.regression
async def test_persistent_state_across_calls():
    r1 = await pymod.exec_python("counter = 5")
    assert r1["success"] is True
    r2 = await pymod.exec_python("counter += 10\ncounter")
    assert r2["success"] is True
    assert r2["result"] == 15


async def test_stderr_captured():
    r = await pymod.exec_python("import sys; sys.stderr.write('warn!')")
    assert r["success"] is True
    assert "warn!" in r["stderr"]


async def test_result_jsonable_container():
    r = await pymod.exec_python("{'a': 1, 'b': [1, 2, 3]}")
    assert r["success"] is True
    assert r["result"] == {"a": 1, "b": [1, 2, 3]}


async def test_extended_imports_absent_is_not_an_error():
    # extended_imports pre-imports numpy/pandas only if present; missing must be
    # silently ignored, not raise.
    r = await pymod.exec_python("print('ok')", extended_imports=True)
    assert r["success"] is True


async def test_resource_limit_too_many_lines():
    code = "\n".join("x = 1" for _ in range(2001))
    r = await pymod.exec_python(code)
    assert r["success"] is False
    blob = ((r.get("error") or {}).get("message", "") + r.get("output", "")).lower()
    assert "lines" in blob or "limit" in blob


async def test_resource_limit_source_too_large():
    code = "x = '" + ("a" * 100_001) + "'"
    r = await pymod.exec_python(code)
    assert r["success"] is False


@pytest.mark.regression
async def test_timeout_kills_and_resets_state(monkeypatch):
    # REGRESSION: a timeout must actually kill the worker (real kill, not a thread
    # nudge) and reset persistent state on the next call. (python_runner design)
    monkeypatch.setattr(pymod, "EXECUTION_TIMEOUT_SECONDS", 2)
    await pymod.exec_python("survivor = 123")
    r = await pymod.exec_python("import time; time.sleep(30)")
    assert r["success"] is False
    blob = ((r.get("error") or {}).get("message", "") + r.get("output", "")).lower()
    assert "timeout" in blob or "time" in blob
    r2 = await pymod.exec_python("'survivor' in dir()")
    assert r2["result"] is False


@pytest.mark.regression
async def test_worker_crash_recovers():
    # REGRESSION: a hard worker exit (os._exit / OOM) must be reported cleanly and
    # the next call must respawn a fresh worker. (_python_worker)
    r = await pymod.exec_python("import os; os._exit(0)")
    assert r["success"] is False
    r2 = await pymod.exec_python("1 + 1")
    assert r2["success"] is True
    assert r2["result"] == 2


@pytest.mark.regression
async def test_fd1_write_does_not_corrupt_protocol():
    # REGRESSION: user code writing raw bytes to fd 1 must not corrupt the
    # newline-JSON worker protocol (the worker redirects fd 1 to fd 2). (_python_worker)
    r = await pymod.exec_python("import os; os.write(1, b'raw-bytes'); print('after')")
    assert r["success"] is True
    assert "after" in r["stdout"]
