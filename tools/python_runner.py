"""
Python execution tool - unlocked execution with resource guards.

User code runs in a persistent worker subprocess (tools/_python_worker.py), not
inline on the server's event loop. That buys three things the old inline runner
could not: a real kill on timeout (SIGKILL, not a best-effort thread nudge),
isolation from the server's stdout / asyncio loop, and module-level state that
persists across calls (the worker is reused). Accident-guards (source-size and
AST-complexity limits) run here in the parent before code is handed to the
worker; a hard memory cap is applied inside the worker on POSIX (RLIMIT_AS).
These guards are seatbelts against runaway model output, not a security sandbox
- this tool intentionally allows all imports and file access.
"""
import os
import sys
import json
import asyncio
import subprocess
import contextlib
import traceback
import ast
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

# ============================================================
#   AUDIT LOGGING
# ============================================================

# Audit is ON by default. Repo-relative path so it never writes outside the repo.
# Relocate with EXEC_AUDIT_ROOT; disable entirely with SHELL0_AUDIT_DISABLE=1.
_DEFAULT_EXEC_AUDIT_ROOT = Path(__file__).resolve().parent.parent / "data" / "exec_audit"
AUDIT_DISABLED = os.getenv("SHELL0_AUDIT_DISABLE", "").strip().lower() in ("1", "true", "yes", "on")
EXEC_AUDIT_ROOT = Path(os.getenv("EXEC_AUDIT_ROOT", str(_DEFAULT_EXEC_AUDIT_ROOT)))
EXEC_AUDIT_MAX_SIZE_MB = int(os.getenv("EXEC_AUDIT_MAX_MB", "50"))
EXEC_AUDIT_SESSION_DIR: Optional[Path] = None


def _get_exec_audit_session_dir() -> Path:
    """Get or create the current session's audit directory."""
    global EXEC_AUDIT_SESSION_DIR
    if EXEC_AUDIT_SESSION_DIR is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        EXEC_AUDIT_SESSION_DIR = EXEC_AUDIT_ROOT / timestamp
        EXEC_AUDIT_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _prune_old_exec_sessions()
    return EXEC_AUDIT_SESSION_DIR


def _get_exec_audit_folder_size() -> int:
    """Get total size of audit folder in bytes."""
    total = 0
    if EXEC_AUDIT_ROOT.exists():
        for f in EXEC_AUDIT_ROOT.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def _prune_old_exec_sessions() -> None:
    """Delete oldest session folders until under size limit (FIFO)."""
    max_bytes = EXEC_AUDIT_MAX_SIZE_MB * 1024 * 1024
    while _get_exec_audit_folder_size() > max_bytes:
        sessions = sorted([d for d in EXEC_AUDIT_ROOT.iterdir() if d.is_dir()])
        if len(sessions) <= 1:
            break
        oldest = sessions[0]
        try:
            shutil.rmtree(oldest)
        except OSError:
            break


def _log_python_exec(code: str, success: bool = True, error: str = "") -> None:
    """Log Python code execution to audit."""
    if AUDIT_DISABLED:
        return
    try:
        session_dir = _get_exec_audit_session_dir()
        timestamp = datetime.now().strftime("%H-%M-%S_%f")
        status = "ok" if success else "err"
        audit_file = session_dir / f"{timestamp}_python_{status}.py"

        with open(audit_file, "w", encoding="utf-8") as f:
            f.write(f"# AUDIT: Python execution\n")
            f.write(f"# Timestamp: {datetime.now().isoformat()}\n")
            f.write(f"# Success: {success}\n")
            if error:
                f.write(f"# Error: {error}\n")
            f.write(f"# {'='*70}\n\n")
            f.write(code)
    except Exception:
        pass  # Don't fail execution due to audit logging

# ============================================================
#   RESOURCE LIMITS (accident guards, not a sandbox)
# ============================================================

MAX_SOURCE_CHARS = 100_000     # 100k chars
MAX_SOURCE_LINES = 2_000       # 2k lines
MAX_AST_NODES    = 20_000      # 20k nodes
MAX_AST_DEPTH    = 100         # depth limit
EXECUTION_TIMEOUT_SECONDS = max(1, int(os.getenv("PYTHON_EXEC_TIMEOUT", "30")))  # 30s default
MAX_MEMORY_BYTES = int(os.getenv("PYTHON_EXEC_MAX_MEMORY", str(8 * 1024 * 1024 * 1024)))  # 8GB (worker-enforced, POSIX)


class ResourceLimitError(Exception):
    """Raised when code exceeds resource limits."""
    pass


def _check_source_limits(code: str) -> None:
    """Pre-parse sanity checks on raw source size."""
    if len(code) > MAX_SOURCE_CHARS:
        raise ResourceLimitError(
            f"Source too large ({len(code)} chars > {MAX_SOURCE_CHARS} limit)."
        )
    line_count = code.count("\n") + 1
    if line_count > MAX_SOURCE_LINES:
        raise ResourceLimitError(
            f"Too many lines ({line_count} > {MAX_SOURCE_LINES} limit)."
        )


def _ast_depth_iterative(node) -> int:
    """Compute max depth of an AST tree using an explicit stack."""
    max_depth = 0
    stack = [(node, 0)]
    while stack:
        current, depth = stack.pop()
        max_depth = max(max_depth, depth)
        if not hasattr(current, "_fields"):
            continue
        for name in current._fields:
            child = getattr(current, name)
            if isinstance(child, ast.AST):
                stack.append((child, depth + 1))
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, ast.AST):
                        stack.append((item, depth + 1))
    return max_depth


def _check_ast_limits(module: ast.AST) -> None:
    """Enforce AST-level resource constraints."""
    node_count = sum(1 for _ in ast.walk(module))
    if node_count > MAX_AST_NODES:
        raise ResourceLimitError(
            f"Code too complex ({node_count} AST nodes > {MAX_AST_NODES} limit)."
        )

    try:
        depth = _ast_depth_iterative(module)
        if depth > MAX_AST_DEPTH:
            raise ResourceLimitError(
                f"Code too deeply nested (depth {depth} > {MAX_AST_DEPTH} limit)."
            )
    except RecursionError:
        raise ResourceLimitError("Code structure too deeply nested.")

# ============================================================
#   AUTO-FIXER FOR MALFORMED PYTHON
# ============================================================

def _maybe_unwrap_quotes(code: str) -> str:
    if code.startswith(("'''", '"""')) and code.endswith(("'''", '"""')):
        return code[3:-3]
    if (code.startswith('"') and code.endswith('"')) or (code.startswith("'") and code.endswith("'")):
        return code[1:-1]
    return code


# Recognized fence language tags. Only these get stripped as a language line -
# anything else after ``` is treated as code (a bare `@dataclass` or `pass` on
# the first line must survive). Keep in sync with filesystem.py.
_FENCE_LANGS = {
    "python", "py", "python3", "javascript", "js", "typescript", "ts",
    "json", "yaml", "yml", "bash", "sh", "shell", "zsh", "powershell",
    "ps1", "bat", "cmd", "html", "css", "xml", "toml", "ini", "text",
    "txt", "plaintext", "markdown", "md", "jsx", "tsx", "sql", "c", "cpp",
    "csharp", "java", "rust", "go", "diff", "console",
}


def _strip_code_fences(code: str) -> str:
    """Strip markdown code fences from model output, preserving internal indentation."""
    if not isinstance(code, str):
        return code

    # Only strip if content actually has code fences
    test = code.strip()
    if not (test.startswith("```") or (test.startswith("`") and test.endswith("`"))):
        return code  # No fences, return as-is preserving all whitespace

    # Has fences - process them
    if test.startswith("```"):
        inner = test[3:].lstrip()  # Remove ``` and any space before language
        if "\n" in inner:
            first_line, rest = inner.split("\n", 1)
            lang = first_line.strip().lower()
            if lang in _FENCE_LANGS:
                inner = rest
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3]
        return inner  # Preserve internal indentation

    # Single backtick case (inline code) - exactly one pair, so code that
    # merely starts and ends with template-literal-style backticks survives
    if test.startswith("`") and test.endswith("`") and test.count("`") == 2:
        return test[1:-1]

    return code


def _parses(code: str) -> bool:
    try:
        ast.parse(code, mode="exec")
        return True
    except Exception:
        return False


def _auto_fix(code: str) -> str:
    """
    Repair-only-if-broken, keep-only-if-verified (same contract as the
    filesystem.py autofixer). Code that already parses passes through
    untouched, with one exception: a module that is nothing but a single
    multi-line string literal is almost certainly quote-wrapped code, so it
    gets unwrapped if the inner text parses. Broken code gets fence-stripping
    then quote-unwrapping; a fix is kept only if the result parses.

    NOTE: Python source deliberately gets no true/false/null -> True/False/None
    coercion. Bare true/false/null parse fine as names, and rewriting them
    would corrupt those tokens inside string and triple-quoted literals; a
    parse gate would never trigger it anyway.
    """
    if not isinstance(code, str):
        return code

    if _parses(code):
        try:
            module = ast.parse(code, mode="exec")
            if (len(module.body) == 1
                    and isinstance(module.body[0], ast.Expr)
                    and isinstance(module.body[0].value, ast.Constant)
                    and isinstance(module.body[0].value.value, str)
                    and "\n" in module.body[0].value.value
                    and _parses(module.body[0].value.value)):
                return module.body[0].value.value
        except Exception:
            pass
        return code

    candidate = _strip_code_fences(code)
    if candidate != code and _parses(candidate):
        return candidate

    candidate = _maybe_unwrap_quotes(code)
    if candidate != code and _parses(candidate):
        return candidate

    return code

# ============================================================
#   ERROR PAYLOAD HELPERS
# ============================================================

class Timeout(Exception):
    pass


def _build_error_payload(exc, stdout: str, stderr: str, fallback_output: str = "") -> dict:
    tb = traceback.format_exc() if not isinstance(exc, Timeout) else None
    if tb == "NoneType: None\n":
        tb = None
    message = str(exc)
    error_dict = {
        "type": exc.__class__.__name__,
        "message": message,
        "traceback": tb,
    }
    output = fallback_output or stdout or stderr or message
    return {
        "success": False,
        "error": error_dict,
        "stdout": stdout,
        "stderr": stderr,
        "output": output,
    }

# ============================================================
#   PERSISTENT EXECUTION WORKER (subprocess)
# ============================================================

_WORKER_SCRIPT = str(Path(__file__).resolve().parent / "_python_worker.py")
_READLINE_LIMIT = 256 * 1024 * 1024   # allow large single-line JSON responses
_WORKER_START_TIMEOUT = 30.0

_worker = None                    # asyncio.subprocess.Process | None
_worker_lock = asyncio.Lock()     # serialize access to the single reused worker


def _is_windows() -> bool:
    return platform.system().lower().startswith("win")


async def _spawn_worker():
    kwargs = {}
    if _is_windows():
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", _WORKER_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=os.getcwd(),
        env=env,
        limit=_READLINE_LIMIT,
        **kwargs,
    )
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=_WORKER_START_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        raise RuntimeError("python_exec worker did not signal ready in time")
    if not line:
        raise RuntimeError("python_exec worker exited before signaling ready")
    msg = json.loads(line.decode("utf-8", errors="replace"))
    if not msg.get("ready"):
        with contextlib.suppress(Exception):
            proc.kill()
        raise RuntimeError(f"python_exec worker failed to start: {msg}")
    return proc


async def _ensure_worker():
    global _worker
    if _worker is None or _worker.returncode is not None:
        _worker = await _spawn_worker()
    return _worker


async def _await_proc_exit(proc) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5.0)


def _reset_worker() -> None:
    global _worker
    proc = _worker
    _worker = None
    if proc is None:
        return
    with contextlib.suppress(Exception):
        proc.kill()
    # Drain the killed process on the running loop so its pipes close cleanly
    # (avoids asyncio "Event loop is closed" transport GC warnings on shutdown).
    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().create_task(_await_proc_exit(proc))

# ============================================================
#   MAIN EXECUTOR (async)
# ============================================================

async def exec_python(code: str, extended_imports: bool = False) -> dict:
    """
    Execute Python code in a persistent worker subprocess.

    State persists across calls (module-level variables, imports, defined
    functions/classes) because a single worker process is reused. A timeout
    kills the worker for real; its persistent namespace is reset on the next
    call.

    Args:
        code: Python source. Use print() for output; the last expression's
              value is returned in 'result'.
        extended_imports: pre-import numpy (np) and pandas (pd) if installed.

    Returns dict: {success, stdout, stderr, result, output} on success, or
    {success: False, error, stdout, stderr, output} on failure/timeout.
    """
    original_code = code

    # Guard + normalize in the parent so there is a single audit point. (This
    # also fixes the old gap where AST-limit rejections skipped the audit.)
    try:
        code = _auto_fix(code)
        _check_source_limits(code)
        module = ast.parse(code, mode="exec")
        _check_ast_limits(module)
    except ResourceLimitError as exc:
        _log_python_exec(original_code, success=False, error=str(exc))
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))
    except SyntaxError as exc:
        _log_python_exec(original_code, success=False, error=str(exc))
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=traceback.format_exc())
    except Exception as exc:
        _log_python_exec(original_code, success=False, error=str(exc))
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=traceback.format_exc())

    request = json.dumps(
        {"code": code, "extended_imports": bool(extended_imports)},
        ensure_ascii=False,
    ) + "\n"

    async with _worker_lock:
        try:
            worker = await _ensure_worker()
        except Exception as exc:
            _reset_worker()
            _log_python_exec(original_code, success=False, error=str(exc))
            return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))

        try:
            worker.stdin.write(request.encode("utf-8"))
            await worker.stdin.drain()
        except Exception as exc:
            _reset_worker()
            _log_python_exec(original_code, success=False, error=str(exc))
            return _build_error_payload(
                exc, stdout="", stderr="",
                fallback_output="python_exec worker write failed: " + str(exc),
            )

        try:
            line = await asyncio.wait_for(
                worker.stdout.readline(), timeout=float(EXECUTION_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            _reset_worker()  # real kill; persistent namespace resets
            msg = (
                f"Timeout: Execution exceeded limit ({EXECUTION_TIMEOUT_SECONDS}s). "
                "The Python worker was terminated; persistent state was reset."
            )
            _log_python_exec(original_code, success=False, error="Timeout")
            return _build_error_payload(Timeout(msg), stdout="", stderr="", fallback_output=msg)

        if not line:
            _reset_worker()
            msg = (
                "python_exec worker exited unexpectedly (crash, OOM, or os._exit); "
                "persistent state was reset."
            )
            _log_python_exec(original_code, success=False, error=msg)
            return _build_error_payload(RuntimeError(msg), stdout="", stderr="", fallback_output=msg)

    # Map the worker response outside the lock.
    try:
        resp = json.loads(line.decode("utf-8", errors="replace"))
    except Exception as exc:
        _reset_worker()
        _log_python_exec(original_code, success=False, error=f"bad worker response: {exc}")
        return _build_error_payload(
            RuntimeError(f"malformed worker response: {exc}"),
            stdout="", stderr="", fallback_output=str(exc),
        )

    if resp.get("ok"):
        _log_python_exec(original_code, success=True)
        return {
            "success": True,
            "stdout": resp.get("stdout", ""),
            "stderr": resp.get("stderr", ""),
            "result": resp.get("result"),
            "output": resp.get("output", ""),
        }

    _log_python_exec(original_code, success=False, error=resp.get("error_message", ""))
    return {
        "success": False,
        "error": {
            "type": resp.get("error_type", "Error"),
            "message": resp.get("error_message", ""),
            "traceback": resp.get("traceback"),
        },
        "stdout": resp.get("stdout", ""),
        "stderr": resp.get("stderr", ""),
        "output": resp.get("output", ""),
    }
