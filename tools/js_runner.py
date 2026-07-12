"""
JavaScript execution tool - unlocked execution via Node.js.

Mirror of the Python runner shape but runs user code in a Node subprocess with
lightweight resource guards and persistent state on disk.
"""
import asyncio
import contextlib
import os
import subprocess
import tempfile
import shutil
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
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))

    cwd = os.getcwd()
    state_path = os.path.join(cwd, STATE_FILE_NAME)
    script_path = None

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
            process.kill()
            with contextlib.suppress(Exception):
                await process.communicate()
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
            return {
                "success": False,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "output": combined_output or error_message,
                "error": {"type": "ProcessError", "message": error_message},
                "exitCode": exit_code,
                "outputTruncated": output_truncated,
            }

        return {
            "success": True,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "output": combined_output,
            "exitCode": exit_code,
            "outputTruncated": output_truncated,
        }

    except Exception as exc:
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))

    finally:
        if script_path and os.path.exists(script_path):
            with contextlib.suppress(Exception):
                os.remove(script_path)
