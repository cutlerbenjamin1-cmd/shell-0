"""
JavaScript execution tool - unlocked execution via Node.js.

Mirror of the Python runner shape but runs user code in a Node subprocess with
lightweight resource guards and persistent state on disk.
"""
import asyncio
import contextlib
import os
import platform
import signal
import subprocess
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from typing import Tuple

# ============================================================
#   RESOURCE LIMITS (kept for safety)
# ============================================================

MAX_SOURCE_CHARS = 100_000
MAX_SOURCE_LINES = 2_000
MAX_OUTPUT_SIZE = 100 * 1024  # 100KB
EXECUTION_TIMEOUT_SECONDS = int(os.getenv("JS_EXEC_TIMEOUT", "30"))

# ============================================================
#   PERSISTENT STATE
# ============================================================

STATE_FILE_NAME = ".shell0_js_state.json"


# ============================================================
#   AUDIT LOGGING
# ============================================================

# Audit is ON by default. Repo-relative path so it never writes outside the repo.
# Relocate with EXEC_AUDIT_ROOT; disable entirely with SHELL0_AUDIT_DISABLE=1.
_DEFAULT_EXEC_AUDIT_ROOT = Path(__file__).resolve().parent.parent / "data" / "exec_audit"
AUDIT_DISABLED = os.getenv("SHELL0_AUDIT_DISABLE", "").strip().lower() in ("1", "true", "yes", "on")
EXEC_AUDIT_ROOT = Path(os.getenv("EXEC_AUDIT_ROOT", str(_DEFAULT_EXEC_AUDIT_ROOT)))
EXEC_AUDIT_MAX_SIZE_MB = int(os.getenv("EXEC_AUDIT_MAX_MB", "50"))
EXEC_AUDIT_SESSION_DIR = None

# Serialize js_exec so concurrent calls cannot race the shared on-disk state file.
_JS_STATE_LOCK = asyncio.Lock()


def _get_exec_audit_session_dir():
    """Get or create the current session's audit directory.

    Prunes on every call, not just when the session directory is first
    created - otherwise a single long-running process's own audit growth is
    never re-checked against the size cap after the first write.
    """
    global EXEC_AUDIT_SESSION_DIR
    if EXEC_AUDIT_SESSION_DIR is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        EXEC_AUDIT_SESSION_DIR = EXEC_AUDIT_ROOT / timestamp
        EXEC_AUDIT_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    _prune_old_exec_sessions()
    return EXEC_AUDIT_SESSION_DIR


def _get_exec_audit_folder_size():
    """Total size of the audit folder in bytes."""
    total = 0
    if EXEC_AUDIT_ROOT.exists():
        for f in EXEC_AUDIT_ROOT.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def _prune_old_exec_sessions():
    """Delete the oldest audit files (FIFO by mtime) until under the size cap.

    Runs at individual-file granularity rather than whole session folders, so
    a single long-running session that alone exceeds the cap gets pruned too -
    not just stale folders left over from previous runs. Always keeps at
    least one file, and never removes the directory currently in use for this
    process's own session - even though it is momentarily empty right after
    creation, a file is about to be written into it by the caller.
    """
    max_bytes = EXEC_AUDIT_MAX_SIZE_MB * 1024 * 1024
    while _get_exec_audit_folder_size() > max_bytes:
        try:
            files = [f for f in EXEC_AUDIT_ROOT.rglob("*") if f.is_file()]
        except OSError:
            break
        if len(files) <= 1:
            break
        try:
            oldest = min(files, key=lambda f: f.stat().st_mtime)
            parent = oldest.parent
            oldest.unlink()
            if parent != EXEC_AUDIT_SESSION_DIR and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            break


def _log_js_exec(code: str, success: bool = True, error: str = "") -> None:
    """Log JavaScript execution to audit."""
    if AUDIT_DISABLED:
        return
    try:
        session_dir = _get_exec_audit_session_dir()
        timestamp = datetime.now().strftime("%H-%M-%S_%f")
        status = "ok" if success else "err"
        audit_file = session_dir / f"{timestamp}_js_{status}.js"
        with open(audit_file, "w", encoding="utf-8") as f:
            print("// AUDIT: JavaScript execution", file=f)
            print(f"// Timestamp: {datetime.now().isoformat()}", file=f)
            print(f"// Success: {success}", file=f)
            if error:
                print(f"// Error: {error}", file=f)
            print("// " + "=" * 70, file=f)
            print(file=f)
            f.write(code)
    except Exception:
        pass  # Don't fail execution due to audit logging


class ResourceLimitError(Exception):
    """Raised when code exceeds configured resource limits."""


# Recognized fence language tags. Only these get stripped as a language line -
# a bare `debugger;` or identifier on the first line must survive.
# Keep in sync with filesystem.py / python_runner.py.
_FENCE_LANGS = {
    "python", "py", "python3", "javascript", "js", "typescript", "ts",
    "json", "yaml", "yml", "bash", "sh", "shell", "zsh", "powershell",
    "ps1", "bat", "cmd", "html", "css", "xml", "toml", "ini", "text",
    "txt", "plaintext", "markdown", "md", "jsx", "tsx", "sql", "c", "cpp",
    "csharp", "java", "rust", "go", "diff", "console", "node",
}


def _strip_code_fences(code: str) -> str:
    """Remove markdown code fences from model output."""
    if not isinstance(code, str):
        return code

    stripped = code.strip()

    if stripped.startswith("```"):
        inner = stripped[3:].lstrip()
        if "\n" in inner:
            first_line, rest = inner.split("\n", 1)
            lang = first_line.strip().lower()
            if lang in _FENCE_LANGS:
                inner = rest
        stripped = inner
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

    # Exactly one backtick pair only - one-line code that merely starts and
    # ends with template literals (`a` + `b`) must survive
    if (stripped.startswith("`") and stripped.endswith("`")
            and "\n" not in stripped and stripped.count("`") == 2):
        stripped = stripped[1:-1].strip()

    return stripped


def _check_source_limits(code: str) -> None:
    """Validate source size and line count before execution."""
    if len(code) > MAX_SOURCE_CHARS:
        raise ResourceLimitError(
            f"Source too large ({len(code)} chars > {MAX_SOURCE_CHARS} limit)."
        )

    line_count = code.count("\n") + 1
    if line_count > MAX_SOURCE_LINES:
        raise ResourceLimitError(
            f"Too many lines ({line_count} > {MAX_SOURCE_LINES} limit)."
        )


def _build_error_payload(exc: Exception, stdout: str, stderr: str, fallback_output: str = "") -> dict:
    message = str(exc)
    error_dict = {
        "type": exc.__class__.__name__,
        "message": message,
    }
    output = fallback_output or stdout or stderr or message
    return {
        "success": False,
        "error": error_dict,
        "stdout": stdout,
        "stderr": stderr,
        "output": output,
    }


def _wrap_code_with_state(code: str, state_path: str) -> str:
    """Inject persistent state load/save around the user code.

    Injected names are __shell0-prefixed and `state` lives on globalThis, so
    user code is free to declare its own `const fs = require('fs')` (or even
    its own `let state`) without identifier collisions.
    """
    escaped_path = state_path.replace("\\", "\\\\")
    return f"""const __shell0_fs = require('fs');
const __SHELL0_STATE_PATH = "{escaped_path}";
globalThis.state = {{}};
try {{ globalThis.state = JSON.parse(__shell0_fs.readFileSync(__SHELL0_STATE_PATH, 'utf-8')); }} catch {{}}

// === User Code ===
{code}

// === Save State ===
try {{ __shell0_fs.writeFileSync(__SHELL0_STATE_PATH, JSON.stringify(globalThis.state, null, 2)); }} catch {{}}
"""


def _truncate_output(text: str) -> Tuple[str, bool]:
    """Trim output to MAX_OUTPUT_SIZE and flag if truncated."""
    if len(text) <= MAX_OUTPUT_SIZE:
        return text, False
    return text[:MAX_OUTPUT_SIZE], True


async def _kill_node_proc(proc: "asyncio.subprocess.Process") -> None:
    """Kill the node process AND anything it spawned (e.g. child_process.spawn
    from user code), not just the immediate PID.

    Mirrors terminal_exec._kill_proc: a bare proc.kill() only signals the node
    process itself. On Windows that leaves grandchildren orphaned; on POSIX,
    SIGKILL can't be caught or forwarded, so a killed shell/node parent never
    gets a chance to propagate it to children it spawned. The process is
    started with start_new_session=True (POSIX) precisely so it has its own
    process group here to kill as a unit.
    """
    if platform.system() == "Windows" and proc.pid:
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
    elif proc.pid:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def exec_js(code: str, timeout: float = None) -> dict:
    """
    Execute JavaScript code via Node.js. UNLOCKED.

    Args:
        code: JavaScript source to execute.
        timeout: Optional timeout override in seconds.

    Returns:
        dict with keys {success, output, stdout, stderr, error?, exitCode?}
    """
    node_path = shutil.which("node")
    if not node_path:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "output": "Node.js executable not found in PATH.",
            "error": {"type": "NodeNotFound", "message": "Node.js executable not found in PATH."},
        }

    try:
        code = _strip_code_fences(code)
        _check_source_limits(code)
    except Exception as exc:
        _log_js_exec(code, success=False, error=str(exc))
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))

    cwd = os.getcwd()
    state_path = os.path.join(cwd, STATE_FILE_NAME)
    script_path = None

    await _JS_STATE_LOCK.acquire()
    try:
        wrapped_code = _wrap_code_with_state(code, state_path)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8", dir=cwd
        ) as tmp:
            tmp.write(wrapped_code)
            script_path = tmp.name

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        popen_kwargs = {"cwd": cwd}
        if creationflags:
            popen_kwargs["creationflags"] = creationflags
        else:
            # POSIX: run node in its own process group so a timeout kill can
            # take out anything it spawned (see _kill_node_proc), not just
            # the node process itself.
            popen_kwargs["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(
            node_path,
            script_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs,
        )

        timeout_seconds = float(timeout) if timeout is not None else float(EXECUTION_TIMEOUT_SECONDS)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout_seconds)
        except asyncio.TimeoutError as exc:
            await _kill_node_proc(process)
            with contextlib.suppress(Exception):
                await process.communicate()
            _log_js_exec(code, success=False, error="Timeout")
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "output": f"Execution timed out after {timeout_seconds} seconds.",
                "error": {"type": "Timeout", "message": str(exc)},
                "exitCode": -1,
            }

        exit_code = process.returncode
        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace")

        stdout_text, stdout_truncated = _truncate_output(stdout_text)
        stderr_text, stderr_truncated = _truncate_output(stderr_text)
        output_truncated = stdout_truncated or stderr_truncated

        combined_output = stdout_text or stderr_text

        if exit_code != 0:
            error_message = stderr_text or f"Process exited with code {exit_code}"
            _log_js_exec(code, success=False, error=error_message)
            return {
                "success": False,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "output": combined_output or error_message,
                "error": {"type": "ProcessError", "message": error_message},
                "exitCode": exit_code,
                "outputTruncated": output_truncated,
            }

        _log_js_exec(code, success=True)
        return {
            "success": True,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "output": combined_output,
            "exitCode": exit_code,
            "outputTruncated": output_truncated,
        }

    except Exception as exc:
        _log_js_exec(code, success=False, error=str(exc))
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))

    finally:
        _JS_STATE_LOCK.release()
        if script_path and os.path.exists(script_path):
            with contextlib.suppress(Exception):
                os.remove(script_path)
