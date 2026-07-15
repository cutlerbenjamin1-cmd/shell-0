"""Shared pytest fixtures for the shell-0 suite.

Isolation model. These tools are deliberately unsandboxed and have real side
effects, so the suite must keep itself hermetic:

  * Audit is OFF by default (and redirected into tmp_path when on). Left on, the
    read/grep/search logging would write the query text into access.log and then
    grep/search would walk that log and match its own query - self-contamination.
    Tests that assert on audit output request the `audit_on` fixture.
  * The persistent python worker and the module-level asyncio locks are recreated
    per test. pytest-asyncio gives each test function its own event loop; a lock
    or subprocess bound to a previous (now-closed) loop would otherwise raise
    "bound to a different event loop" the second time it is touched. Recreating
    them at setup - before this test's loop starts - lets each bind fresh.
"""
import asyncio
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Windows needs the Proactor loop for asyncio subprocesses (the python worker,
# the js/terminal children, and the stdio client all spawn processes; the
# Selector loop raises NotImplementedError on subprocess transports).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import tools.filesystem as filesystem
import tools.python_runner as python_runner
import tools.js_runner as js_runner
import tools.terminal_exec as terminal_exec

NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def isolate_audit(tmp_path, monkeypatch):
    """Redirect every tool's forensic audit into this test's tmp dir, OFF by default.

    Returned so `audit_on` (and audit-behaviour tests) can read the logs back.
    """
    fs_audit = tmp_path / "fs_audit"
    exec_audit = tmp_path / "exec_audit"
    monkeypatch.setattr(filesystem, "AUDIT_ROOT", fs_audit)
    monkeypatch.setattr(filesystem, "AUDIT_SESSION_DIR", None)
    monkeypatch.setattr(filesystem, "AUDIT_DISABLED", True)
    for mod in (python_runner, js_runner, terminal_exec):
        monkeypatch.setattr(mod, "EXEC_AUDIT_ROOT", exec_audit)
        monkeypatch.setattr(mod, "EXEC_AUDIT_SESSION_DIR", None)
        monkeypatch.setattr(mod, "AUDIT_DISABLED", True)
    return {"fs_audit": fs_audit, "exec_audit": exec_audit}


@pytest.fixture
def audit_on(isolate_audit, monkeypatch):
    """Enable the forensic audit (still redirected to tmp) for audit-behaviour tests."""
    for mod in (filesystem, python_runner, js_runner, terminal_exec):
        monkeypatch.setattr(mod, "AUDIT_DISABLED", False)
    return isolate_audit


@pytest.fixture(autouse=True)
def _fresh_async_primitives(monkeypatch):
    """Recreate loop-bound globals per test, and kill the worker between tests."""
    monkeypatch.setattr(python_runner, "_worker_lock", asyncio.Lock())
    monkeypatch.setattr(js_runner, "_JS_STATE_LOCK", asyncio.Lock())
    python_runner._reset_worker()
    yield
    python_runner._reset_worker()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """chdir into an isolated dir. js_exec writes its state file and temp scripts
    into os.getcwd(), so js tests must run from a throwaway cwd."""
    d = tmp_path / "work"
    d.mkdir()
    monkeypatch.chdir(d)
    return d
