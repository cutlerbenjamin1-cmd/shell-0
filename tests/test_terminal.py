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


# ---------------------- 2026-07-16 audit pass regressions -------------------

def test_kill_proc_is_a_coroutine():
    # REGRESSION: _kill_proc once called blocking subprocess.run(taskkill) inline,
    # stalling the single-threaded event loop (every in-flight MCP tool call) for
    # up to its 5s timeout on every foreground timeout and every bg_kill. It must
    # be a coroutine that offloads taskkill to a thread. (audit 2026-07-16)
    assert asyncio.iscoroutinefunction(termmod._kill_proc)


@pytest.mark.regression
async def test_background_launch_is_audited_immediately(audit_on):
    # REGRESSION: background tasks were only audited at completion/kill, so a
    # server crash mid-task left zero trace the task ever ran. The launch itself
    # must be logged synchronously, before the task_id is returned. (audit 2026-07-16)
    exec_audit = audit_on["exec_audit"]
    r = await termmod.terminal_exec(
        command=f'"{sys.executable}" -c "import time; time.sleep(30)"',
        run_in_background=True,
    )
    assert r["success"] is True and r["background"] is True
    tid = r["task_id"]
    try:
        # the launch record exists NOW, long before the 30s sleeper could finish
        records = list(exec_audit.rglob("*terminal*"))
        blob = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in records)
        assert "Background task" in blob
        assert tid in blob
    finally:
        await termmod.terminal_exec(bg_kill=tid)


@pytest.mark.regression
async def test_timeout_defaults_bg_600_fg_120(monkeypatch):
    # REGRESSION: timeout defaulted to 120s and that same value was handed to the
    # background collector, so background jobs were silently killed at 120s. It is
    # now Optional and resolves to 600s background / 120s foreground. Both paths
    # funnel the resolved value into asyncio.wait_for, so capture it there - that
    # is the timeout that actually governs the process. (commit 84efe0e)
    seen = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(aw, timeout=None, **kw):
        seen.append(timeout)
        return await real_wait_for(aw, timeout=timeout)
    monkeypatch.setattr(termmod.asyncio, "wait_for", spy_wait_for)

    # foreground, no explicit timeout -> resolves to 120
    r = await termmod.terminal_exec(command="echo fg_default")
    assert r["success"] is True
    assert 120 in seen

    # background, no explicit timeout -> the collector must get 600, never 120
    seen.clear()
    r = await termmod.terminal_exec(command="echo bg_default", run_in_background=True)
    assert r["success"] is True and r["background"] is True
    for _ in range(50):
        st = await termmod.terminal_exec(bg_status=r["task_id"])
        if st["status"] != "running":
            break
        await asyncio.sleep(0.1)
    assert 600 in seen
    assert 120 not in seen
