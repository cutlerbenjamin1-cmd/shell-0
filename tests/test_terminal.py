"""Coverage + regression tests for the terminal tool (tools/terminal_exec.py).

Commands are kept intentionally boring (echo / python one-liners / a controlled
sleeper) - the tool is unsandboxed, so the suite must never run anything
destructive.
"""
import asyncio
import sys
import time

import pytest

import tools.terminal_exec as termmod


async def test_echo_output():
    r = await termmod.terminal_exec(command="echo hello123")
    assert r["success"] is True
    assert "hello123" in r["output"]


async def test_exit_code_nonzero():
    r = await termmod.terminal_exec(command="exit 3")
    assert r["success"] is False
    assert r["exit_code"] == 3


@pytest.mark.regression
async def test_no_output_command_returns_quickly():
    # REGRESSION: a successful command with no stdout once hung the invisible
    # terminal forever - the child inherited the MCP stdin (the JSON-RPC channel).
    # With stdin=DEVNULL it must return promptly with a completion sentinel.
    # (session 4c9451c7, 2025-11-30)
    start = time.time()
    r = await termmod.terminal_exec(command="cd .", timeout=30)
    assert r["success"] is True
    assert time.time() - start < 15
    assert "completed" in r["output"].lower()


@pytest.mark.regression
async def test_python_dash_c_returns():
    # REGRESSION: `python -c` on success was the canonical hang repro.
    r = await termmod.terminal_exec(command=f'"{sys.executable}" -c "print(2 + 2)"', timeout=30)
    assert r["success"] is True
    assert "4" in r["output"]


@pytest.mark.regression
async def test_timeout_returns_clean():
    # REGRESSION: a timeout must return a clean error and kill the process tree,
    # not hang. (audit 8df5b5e1: proc.kill once only killed the shell)
    start = time.time()
    r = await termmod.terminal_exec(
        command=f'"{sys.executable}" -c "import time; time.sleep(30)"', timeout=2
    )
    assert r["success"] is False
    assert "timed out" in r["error"].lower()
    assert time.time() - start < 15


async def test_cwd_is_respected(tmp_path):
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    r = await termmod.terminal_exec(
        command=f'"{sys.executable}" -c "import os; print(os.listdir())"',
        cwd=str(tmp_path),
    )
    assert r["success"] is True
    assert "marker.txt" in r["output"]


async def test_stderr_captured():
    r = await termmod.terminal_exec(command="echo errtext 1>&2")
    assert "errtext" in r["output"]
    assert "[stderr]" in r["output"]  # routed via the stderr channel, not stdout


async def test_background_task_lifecycle():
    r = await termmod.terminal_exec(
        command=f'"{sys.executable}" -c "import time; time.sleep(1); print(987654)"',
        run_in_background=True,
    )
    assert r["success"] is True and r["background"] is True
    tid = r["task_id"]

    status = {"status": "running"}
    for _ in range(50):
        status = await termmod.terminal_exec(bg_status=tid)
        if status["status"] != "running":
            break
        await asyncio.sleep(0.2)
    assert status["status"] == "completed"
    assert "987654" in status["output"]

    listing = await termmod.terminal_exec(bg_list=True)
    assert any(t["task_id"] == tid for t in listing["tasks"])
