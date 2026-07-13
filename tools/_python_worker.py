"""
Persistent Python execution worker for shell-0's python_exec.

Spawned as a long-lived child process by tools/python_runner.py. It keeps one
module-level namespace (GLOBAL_ENV) alive across requests so state persists
between python_exec calls, while isolating execution from the MCP server: a
runaway here can be SIGKILLed without touching the server, and nothing here can
mutate the server's stdout or asyncio loop.

Protocol (newline-delimited JSON, one message per line):
  parent -> worker (stdin):  {"code": "<source>", "extended_imports": false}
                             {"cmd": "shutdown"}
  worker -> parent (stdout): {"ready": true}                       (once, at startup)
                             {"ok": true,  "stdout", "stderr", "result", "output"}
                             {"ok": false, "stdout", "stderr", "error_type",
                              "error_message", "traceback", "output"}

fd 1 is reserved for this protocol. User code's stdout/stderr are captured at
the Python level and returned in the response; direct fd-1 writes are shunted to
fd 2 so they cannot corrupt the protocol.
"""
import os
import sys
import io
import ast
import json
import platform
import contextlib
import traceback
import builtins

MAX_OUTPUT_CHARS = int(os.getenv("PYTHON_EXEC_MAX_OUTPUT", str(1_000_000)))
MAX_MEMORY_BYTES = int(os.getenv("PYTHON_EXEC_MAX_MEMORY", str(8 * 1024 * 1024 * 1024)))

# Reserve fd 1 (the pipe the parent reads) for the JSON protocol, then point the
# process's fd 1 at fd 2 so any *direct* fd-1 writes from user code cannot
# corrupt the protocol. Binary, unbuffered, surrogate-safe writes.
_proto_fd = os.dup(1)
with contextlib.suppress(OSError):
    os.dup2(2, 1)
_proto = os.fdopen(_proto_fd, "wb", buffering=0)

if hasattr(sys.stdin, "reconfigure"):
    with contextlib.suppress(Exception):
        sys.stdin.reconfigure(encoding="utf-8")


def _send(obj) -> None:
    text = json.dumps(obj, ensure_ascii=False) + "\n"
    _proto.write(text.encode("utf-8", errors="replace"))


# --- persistent namespace --------------------------------------------------
GLOBAL_ENV = {}


def _init_env() -> None:
    GLOBAL_ENV.update({
        "__builtins__": dict(vars(builtins)),
        "__name__": "__main__",
        "__doc__": None,
    })


def _apply_memory_limit() -> None:
    """POSIX: cap this worker's address space (it is the sandbox now). No-op on
    Windows, which has no cheap hard cap - documented in the README."""
    if platform.system() == "Windows":
        return
    try:
        import resource
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, hard))
    except Exception:
        pass


def _preload_extended_imports() -> None:
    for module_name, alias in (("numpy", "np"), ("pandas", "pd")):
        if alias in GLOBAL_ENV:
            continue
        try:
            GLOBAL_ENV[alias] = __import__(module_name)
        except Exception:
            pass


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


def _clip(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n... [output clipped at {MAX_OUTPUT_CHARS} chars]"
    return text


def _run_once(code: str, extended_imports: bool) -> dict:
    if extended_imports:
        _preload_extended_imports()

    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()

    try:
        module = ast.parse(code, mode="exec")
        has_result = bool(module.body and isinstance(module.body[-1], ast.Expr))
        if has_result:
            last_expr = module.body[-1].value
            module.body[-1] = ast.Assign(
                targets=[ast.Name(id="_result", ctx=ast.Store())],
                value=last_expr,
            )
            ast.fix_missing_locations(module)
        compiled = compile(module, "<python_exec>", "exec")
    except BaseException as exc:
        tb = traceback.format_exc()
        return {
            "ok": False, "stdout": "", "stderr": "",
            "error_type": exc.__class__.__name__, "error_message": str(exc),
            "traceback": tb, "output": tb,
        }

    # Catch BaseException so a stray sys.exit()/KeyboardInterrupt in user code is
    # reported instead of killing the worker (and wiping the persistent state).
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exec(compiled, GLOBAL_ENV)
    except BaseException as exc:
        stdout_val = _clip(stdout_buf.getvalue())
        stderr_val = _clip(stderr_buf.getvalue())
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        suffix = "" if not stdout_val or stdout_val.endswith("\n") else "\n"
        return {
            "ok": False, "stdout": stdout_val, "stderr": stderr_val,
            "error_type": exc.__class__.__name__, "error_message": str(exc),
            "traceback": tb, "output": stdout_val + suffix + tb,
        }

    stdout_val = _clip(stdout_buf.getvalue())
    stderr_val = _clip(stderr_buf.getvalue())
    result_value = GLOBAL_ENV.pop("_result", None) if has_result else None
    if result_value is None:
        output = stdout_val
    else:
        suffix = "" if not stdout_val or stdout_val.endswith("\n") else "\n"
        output = f"{stdout_val}{suffix}{repr(result_value)}"
    return {
        "ok": True, "stdout": stdout_val, "stderr": stderr_val,
        "result": _to_jsonable(result_value), "output": output,
    }


def main() -> None:
    _init_env()
    _apply_memory_limit()
    _send({"ready": True})

    while True:
        raw = sys.stdin.readline()
        if raw == "":            # parent closed stdin -> shut down
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except Exception as exc:
            _send({
                "ok": False, "stdout": "", "stderr": "",
                "error_type": "ProtocolError", "error_message": f"bad request: {exc}",
                "traceback": "", "output": f"bad request: {exc}",
            })
            continue
        if req.get("cmd") == "shutdown":
            break
        _send(_run_once(req.get("code", ""), bool(req.get("extended_imports", False))))


if __name__ == "__main__":
    main()
