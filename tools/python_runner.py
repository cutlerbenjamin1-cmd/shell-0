"""
Python execution tool - partially unlocked for Claude Code usage.
Keeps resource guards (timeout, size, AST complexity) but allows all imports and file access.
"""
import io
import sys
import contextlib
import traceback
import ast
import builtins
import platform
import threading
import ctypes
import os
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
#   RESOURCE LIMITS (kept for safety)
# ============================================================

MAX_SOURCE_CHARS = 100_000     # 100k chars (was 50k)
MAX_SOURCE_LINES = 2_000       # 2k lines (was 1k)
MAX_AST_NODES    = 20_000      # 20k nodes (was 10k)
MAX_AST_DEPTH    = 100         # depth limit (was 80)
EXECUTION_TIMEOUT_SECONDS = max(1, int(os.getenv("PYTHON_EXEC_TIMEOUT", "30")))  # 30s default (was 10s)
MAX_MEMORY_BYTES = int(os.getenv("PYTHON_EXEC_MAX_MEMORY", str(8 * 1024 * 1024 * 1024)))  # 8GB default


class ResourceLimitError(Exception):
    """Raised when code exceeds resource limits."""
    pass


def _get_process_rss() -> int:
    """Get current process RSS in bytes. Works on Windows and POSIX."""
    try:
        if platform.system() == "Windows":
            # Use kernel32/psapi via ctypes (no psutil dependency)
            from ctypes import wintypes
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize
        else:
            import resource
            # ru_maxrss is in KB on Linux, bytes on macOS
            usage = resource.getrusage(resource.RUSAGE_SELF)
            if platform.system() == "Darwin":
                return usage.ru_maxrss
            return usage.ru_maxrss * 1024
    except Exception:
        pass
    return 0


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
#   GLOBAL PERSISTENT ENVIRONMENT
# ============================================================

GLOBAL_ENV = {}

# ============================================================
#   UNLOCKED GLOBALS (full builtins + real __import__)
# ============================================================

# Start with all standard builtins
UNLOCKED_BUILTINS = dict(vars(builtins))

# Ensure key functions are available
UNLOCKED_BUILTINS.update({
    # These should already be there, but be explicit
    "open": open,
    "input": input,
    "print": print,
    "__import__": __import__,
    
    # File/path helpers
    "exec": exec,
    "eval": eval,
    "compile": compile,
    
    # Common utilities
    "dir": dir,
    "vars": vars,
    "globals": globals,
    "locals": locals,
    "getattr": getattr,
    "setattr": setattr,
    "hasattr": hasattr,
    "delattr": delattr,
    "callable": callable,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "type": type,
    "object": object,
    
    # Iteration/sequences
    "iter": iter,
    "next": next,
    "reversed": reversed,
    "slice": slice,
    
    # I/O
    "format": format,
    "repr": repr,
    "ascii": ascii,
    "chr": chr,
    "ord": ord,
    "bin": bin,
    "hex": hex,
    "oct": oct,
    
    # Memory/object
    "id": id,
    "hash": hash,
    "memoryview": memoryview,
    "bytearray": bytearray,
    "bytes": bytes,
    
    # Class helpers
    "property": property,
    "classmethod": classmethod,
    "staticmethod": staticmethod,
    "super": super,
})

UNLOCKED_GLOBALS = {
    "__builtins__": UNLOCKED_BUILTINS,
    "__name__": "__main__",
    "__doc__": None,
}

# ============================================================
#   NORMALIZE ESCAPED NEWLINES (LM Studio fix)
# ============================================================

def _normalize_escaped_newlines(code: str) -> str:
    if "\\n" in code:
        code = code.replace("\\r\\n", "\n").replace("\\n", "\n")
    return code

# ============================================================
#   AUTO-FIXER FOR MALFORMED PYTHON
# ============================================================

def _maybe_unwrap_quotes(code: str) -> str:
    if code.startswith(("'''", '"""')) and code.endswith(("'''", '"""')):
        return code[3:-3]
    if (code.startswith('"') and code.endswith('"')) or (code.startswith("'") and code.endswith("'")):
        return code[1:-1]
    return code


def _fix_json_style(code: str) -> str:
    import re
    if not any(tok in code for tok in ("true", "false", "null")):
        return code
    try:
        string_spans = [(m.start(), m.end()) for m in re.finditer(r'(["\'])(?:(?=(\\?))\\2.)*?\\1', code)]
        def in_string(pos):
            return any(start <= pos < end for start, end in string_spans)
        def replacer(match):
            if in_string(match.start()):
                return match.group(0)
            word = match.group(0)
            return {"true": "True", "false": "False", "null": "None"}.get(word, word)
        return re.sub(r'\b(true|false|null)\b', replacer, code)
    except Exception:
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

    NOTE: Removed _fix_json_style from the pipeline - its in-string guard
    used mangled backreferences (literal \\1/\\2 instead of backrefs), so it
    rewrote true/false/null INSIDE string literals, and even a corrected
    guard can't protect triple-quoted strings. Bare `true` parses fine
    anyway, so a parse gate would never trigger it.
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
#   JSON SERIALIZATION
# ============================================================

def _to_jsonable(value):
    import pathlib, datetime, base64
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("utf-8")
    if isinstance(value, pathlib.Path):
        return str(value)
    # Handle numpy/pandas if present
    try:
        import numpy as np
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (np.integer, np.floating)):
            return value.item()
    except ImportError:
        pass
    try:
        import pandas as pd
        if isinstance(value, pd.DataFrame):
            return value.to_dict(orient="records")
        if isinstance(value, pd.Series):
            return value.to_dict()
    except ImportError:
        pass
    return repr(value)

# ============================================================
#   ERROR PAYLOAD HELPERS
# ============================================================

def _build_error_payload(exc, stdout: str, stderr: str, fallback_output: str = "") -> dict:
    tb = traceback.format_exc() if not isinstance(exc, Timeout) else None
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
#   TIMEOUT EXCEPTION
# ============================================================

class Timeout(Exception):
    pass

# ============================================================
#   MAIN EXECUTOR (async, LM Studio compatible)
# ============================================================

async def exec_python(code: str, extended_imports: bool = None) -> dict:
    """
    Execute Python code in a persistent environment.
    
    Partially unlocked:
    - All imports allowed (os, subprocess, requests, etc.)
    - File access (open, pathlib)
    - Full builtins (eval, exec, compile)
    
    Still guarded:
    - Source size limits (100k chars, 2k lines)
    - AST complexity limits (20k nodes)
    - Execution timeout (30s default, env PYTHON_EXEC_TIMEOUT)
    
    Args:
        code: Python code to execute
        extended_imports: Ignored (kept for API compatibility)
    """
    return await _exec_python_inner(code)


async def _exec_python_inner(code: str) -> dict:
    """Inner implementation of exec_python."""
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    original_code = code  # Keep original for audit

    try:
        # _auto_fix handles fence-stripping itself (parse-gated)
        code = _auto_fix(code)
        _check_source_limits(code)
    except ResourceLimitError as exc:
        _log_python_exec(original_code, success=False, error=str(exc))
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))
    except Exception as exc:
        _log_python_exec(original_code, success=False, error=str(exc))
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))
    
    # NOTE: Audit logging moved to AFTER execution with actual result

    # Merge UNLOCKED_GLOBALS into GLOBAL_ENV once, so functions defined in exec()
    # have access to builtins and imports in the same namespace (fixes NameError
    # when functions try to access module-level imports like numpy)
    if "__builtins__" not in GLOBAL_ENV:
        GLOBAL_ENV.update(UNLOCKED_GLOBALS)
    
    local_env = GLOBAL_ENV

    try:
        module = ast.parse(code, mode="exec")
        _check_ast_limits(module)
    except ResourceLimitError as exc:
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=str(exc))
    except Exception as exc:
        err = traceback.format_exc()
        return _build_error_payload(exc, stdout="", stderr="", fallback_output=err)

    # Capture last expression as result
    has_result_expr = bool(module.body and isinstance(module.body[-1], ast.Expr))
    if has_result_expr:
        last_expr = module.body[-1].value
        module.body[-1] = ast.Assign(
            targets=[ast.Name(id="_result", ctx=ast.Store())],
            value=last_expr,
        )
        ast.fix_missing_locations(module)

    compiled = compile(module, "<string>", "exec")

    timeout_message = (
        f"Timeout: Execution exceeded limit ({EXECUTION_TIMEOUT_SECONDS}s). "
        "Tip: simplify or shorten loops."
    )

    # Execution state shared with worker thread
    exec_result = {"exception": None, "completed": False}
    
    def _exec_worker():
        """Worker function that runs exec() - can be interrupted on Windows."""
        _call_count = 0
        _baseline_rss = _get_process_rss()

        def _mem_trace(frame, event, arg):
            """Trace hook that checks memory every ~5000 calls."""
            nonlocal _call_count
            _call_count += 1
            if _call_count % 5000 == 0:
                rss = _get_process_rss()
                if rss > MAX_MEMORY_BYTES:
                    raise MemoryError(
                        f"Memory limit exceeded: {rss / (1024**3):.1f}GB > "
                        f"{MAX_MEMORY_BYTES / (1024**3):.0f}GB limit. "
                        f"Reduce data size or use generators/streaming."
                    )
            return _mem_trace

        try:
            sys.settrace(_mem_trace)
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                exec(compiled, local_env)  # Single namespace so function.__globals__ works
            exec_result["completed"] = True
        except Exception as e:
            exec_result["exception"] = e
        finally:
            sys.settrace(None)

    def _interrupt_thread(thread_id: int) -> bool:
        """Attempt to raise KeyboardInterrupt in target thread (Windows)."""
        try:
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_id),
                ctypes.py_object(KeyboardInterrupt)
            )
            return res == 1
        except Exception:
            return False

    # OS-specific timeout handling
    is_windows = platform.system().lower().startswith("win")
    
    if is_windows:
        # Windows: Run in thread with join timeout, interrupt if needed
        exec_thread = threading.Thread(target=_exec_worker, daemon=True)
        exec_thread.start()
        exec_thread.join(timeout=float(EXECUTION_TIMEOUT_SECONDS))
        
        if exec_thread.is_alive():
            # Timeout - try to interrupt the thread
            _interrupt_thread(exec_thread.ident)
            exec_thread.join(timeout=1.0)  # Give it a second to die
            
            stdout_val = stdout_buffer.getvalue()
            stderr_val = stderr_buffer.getvalue()
            _log_python_exec(code, success=False, error="Timeout")
            return _build_error_payload(
                Timeout(timeout_message),
                stdout=stdout_val,
                stderr=stderr_val,
                fallback_output=stdout_val + "\n" + timeout_message,
            )
        
        # Thread completed - check for exception
        if exec_result["exception"]:
            exc = exec_result["exception"]
            stdout_val = stdout_buffer.getvalue()
            stderr_val = stderr_buffer.getvalue()
            err = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            _log_python_exec(code, success=False, error=str(exc))
            return _build_error_payload(
                exc,
                stdout=stdout_val,
                stderr=stderr_val,
                fallback_output=stdout_val + ("" if stdout_val.endswith("\n") else "\n") + err,
            )
    else:
        # POSIX: Use signal.alarm (works in main thread)
        import signal
        import resource
        old_handler = signal.getsignal(signal.SIGALRM)

        # Set memory limit (POSIX only - enforceable at OS level)
        old_soft, old_hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, old_hard))

        def _timeout_handler(signum, frame):
            raise Timeout(timeout_message)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(EXECUTION_TIMEOUT_SECONDS)

        try:
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                exec(compiled, local_env)  # Single namespace so function.__globals__ works
        except Timeout as exc:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            stdout_val = stdout_buffer.getvalue()
            stderr_val = stderr_buffer.getvalue()
            _log_python_exec(code, success=False, error="Timeout")
            return _build_error_payload(
                exc,
                stdout=stdout_val,
                stderr=stderr_val,
                fallback_output=stdout_val + "\n" + str(exc),
            )
        except Exception as exc:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            stdout_val = stdout_buffer.getvalue()
            stderr_val = stderr_buffer.getvalue()
            err = traceback.format_exc()
            _log_python_exec(code, success=False, error=str(exc))
            return _build_error_payload(
                exc,
                stdout=stdout_val,
                stderr=stderr_val,
                fallback_output=stdout_val + ("" if stdout_val.endswith("\n") else "\n") + err,
            )
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            resource.setrlimit(resource.RLIMIT_AS, (old_soft, old_hard))

    # Success path
    stdout_val = stdout_buffer.getvalue()
    stderr_val = stderr_buffer.getvalue()
    result_value = local_env.get("_result") if has_result_expr else None
    json_result = _to_jsonable(result_value)

    if result_value is None:
        combined_output = stdout_val
    else:
        suffix = "" if not stdout_val or stdout_val.endswith("\n") else "\n"
        combined_output = f"{stdout_val}{suffix}{repr(result_value)}"

    # Audit log success AFTER execution completes
    _log_python_exec(code, success=True)
    
    return {
        "success": True,
        "stdout": stdout_val,
        "stderr": stderr_val,
        "result": json_result,
        "output": combined_output,
    }
