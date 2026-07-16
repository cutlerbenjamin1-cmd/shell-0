"""
Terminal execution tool - unrestricted shell command execution.
Simple asyncio subprocess with proper stdin isolation.
"""
import asyncio
import platform
import subprocess
import time
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _log_terminal_exec(command: str, cwd: Optional[str] = None, success: bool = True, 
                       exit_code: int = 0, output: str = "", error: str = "") -> None:
    """Log terminal command execution to audit."""
    if AUDIT_DISABLED:
        return
    try:
        session_dir = _get_exec_audit_session_dir()
        timestamp = datetime.now().strftime("%H-%M-%S_%f")
        status = "ok" if success else "err"
        # Use .sh extension for shell commands
        audit_file = session_dir / f"{timestamp}_terminal_{status}.sh"
        
        with open(audit_file, "w", encoding="utf-8") as f:
            f.write(f"# AUDIT: Terminal execution\n")
            f.write(f"# Timestamp: {datetime.now().isoformat()}\n")
            f.write(f"# CWD: {cwd or 'default'}\n")
            f.write(f"# Success: {success}\n")
            f.write(f"# Exit code: {exit_code}\n")
            if error:
                f.write(f"# Error: {error}\n")
            f.write(f"# {'='*70}\n\n")
            f.write(command)
            if output:
                f.write(f"\n\n# {'='*70}\n# OUTPUT:\n# {'='*70}\n")
                # Prefix each line with # to keep it as comment
                for line in output.split('\n')[:50]:  # Limit output lines
                    f.write(f"# {line}\n")
    except Exception:
        pass  # Don't fail execution due to audit logging


# ============================================================
#   BACKGROUND TASKS
# ============================================================

MAX_BACKGROUND_TASKS = 10
MAX_BG_OUTPUT_BYTES = 1_048_576  # 1MB
BG_CLEANUP_AGE = 1800  # 30 min


@dataclass
class BackgroundTask:
    task_id: str
    command: str
    cwd: Optional[str]
    status: str  # running / completed / failed / timeout / killed
    start_time: float
    output: str = ""
    end_time: Optional[float] = None
    exit_code: Optional[int] = None
    proc: Any = field(default=None, repr=False)
    _collector: Any = field(default=None, repr=False)


BACKGROUND_TASKS: Dict[str, BackgroundTask] = {}


def _bg_cleanup() -> None:
    """Purge completed background tasks older than BG_CLEANUP_AGE."""
    now = time.time()
    expired = [
        tid for tid, t in BACKGROUND_TASKS.items()
        if t.status != "running" and t.end_time and (now - t.end_time) > BG_CLEANUP_AGE
    ]
    for tid in expired:
        del BACKGROUND_TASKS[tid]


def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    """Kill a process, handling Windows process trees."""
    if platform.system() == "Windows" and proc.pid:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=5
            )
        except Exception:
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _cap_output(text: str) -> str:
    """Cap output to MAX_BG_OUTPUT_BYTES, keeping the tail."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_BG_OUTPUT_BYTES:
        return text
    trimmed = encoded[-MAX_BG_OUTPUT_BYTES:]
    omitted = len(encoded) - MAX_BG_OUTPUT_BYTES
    return f"[truncated: {omitted} bytes omitted]\n" + trimmed.decode("utf-8", errors="replace")


async def _bg_collector(task: BackgroundTask, timeout: float) -> None:
    """Collect output from a background process."""
    try:
        stdout, stderr = await asyncio.wait_for(task.proc.communicate(), timeout=timeout)
        stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
        task.exit_code = task.proc.returncode
        task.status = "completed" if task.exit_code == 0 else "failed"

        output = ""
        if stdout_str.strip():
            output = stdout_str.strip()
        if stderr_str.strip():
            output += ("\n\n[stderr]:\n" if output else "[stderr]:\n") + stderr_str.strip()
        if not output:
            output = f"[Command {'completed successfully' if task.exit_code == 0 else 'failed'}]"
        task.output = _cap_output(output)

    except asyncio.TimeoutError:
        _kill_proc(task.proc)
        await task.proc.wait()
        task.status = "timeout"
        task.exit_code = -1
        task.output = f"[Background task timed out after {timeout}s]"

    except Exception as e:
        task.status = "failed"
        task.exit_code = -1
        task.output = f"[Background collector error: {e}]"

    task.end_time = time.time()
    _log_terminal_exec(
        task.command, cwd=task.cwd,
        success=(task.status == "completed"),
        exit_code=task.exit_code or -1,
        output=task.output[:2000],
    )


async def terminal_exec(
    command: str = "",
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    run_in_background: bool = False,
    bg_status: str = "",
    bg_kill: str = "",
    bg_list: bool = False,
    visible: bool = False,  # ignored, kept for API compat
    description: str = "",  # ignored, kept for API compat
) -> Dict:
    """Execute shell commands, with background task management."""
    _bg_cleanup()
    
    if bg_list:
        return _do_bg_list()
    if bg_status:
        return _do_bg_status(bg_status)
    if bg_kill:
        return await _do_bg_kill(bg_kill)
    
    if not command or not isinstance(command, str):
        return {"success": False, "error": "Command must be a non-empty string."}
    
    start = time.time()
    # Foreground defaults to 120s; background defaults to the 600s ceiling so
    # long jobs aren't silently killed at the foreground default.
    if timeout is None:
        timeout = 600.0 if run_in_background else 120.0
    timeout = min(max(timeout, 1), 600)
    
    if run_in_background:
        running = sum(1 for t in BACKGROUND_TASKS.values() if t.status == "running")
        if running >= MAX_BACKGROUND_TASKS:
            return {"success": False, "error": f"Max {MAX_BACKGROUND_TASKS} concurrent background tasks."}
        
        try:
            if platform.system() == "Windows":
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    executable="/bin/bash",
                    cwd=cwd,
                )
        except Exception as e:
            return {"success": False, "error": f"Failed to spawn background task: {e}"}
        
        task_id = uuid.uuid4().hex[:8]
        task = BackgroundTask(
            task_id=task_id, command=command, cwd=cwd,
            status="running", start_time=start, proc=proc,
        )
        task._collector = asyncio.create_task(_bg_collector(task, timeout))
        BACKGROUND_TASKS[task_id] = task
        return {
            "success": True, "background": True, "task_id": task_id,
            "command": command, "cwd": cwd, "pid": proc.pid,
            "message": f"Running in background. Check with terminal_bg_status(task_id='{task_id}').",
        }
    
    try:
        # CRITICAL: stdin=DEVNULL prevents child from inheriting parent's stdin
        # which is the MCP JSON-RPC channel. Without this, Python subprocesses
        # hang because they interfere with MCP communication during initialization.
        if platform.system() == "Windows":
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                executable="/bin/bash",
                cwd=cwd,
            )
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            _kill_proc(proc)
            await proc.wait()
            _log_terminal_exec(command, cwd=cwd, success=False, error=f"Timeout after {timeout}s")
            return {
                "success": False,
                "error": f"Command timed out after {timeout}s",
                "command": command,
                "cwd": cwd,
                "duration_seconds": time.time() - start,
            }
        
        duration = time.time() - start
        success = exit_code == 0
        
        # Build output
        output = ""
        if stdout_str.strip():
            output = stdout_str.strip()
        if stderr_str.strip():
            if output:
                output += f"\n\n[stderr]:\n{stderr_str.strip()}"
            else:
                output = f"[stderr]:\n{stderr_str.strip()}"
        
        if not output:
            output = f"[Command completed {'successfully' if success else 'with errors'}]"
        
        _log_terminal_exec(command, cwd=cwd, success=success, exit_code=exit_code, output=output)
        return {
            "success": success,
            "command": command,
            "cwd": cwd,
            "exit_code": exit_code,
            "output": output,
            "duration_seconds": duration,
        }
    
    except Exception as e:
        _log_terminal_exec(command, cwd=cwd, success=False, error=str(e))
        return {
            "success": False,
            "error": str(e),
            "command": command,
            "cwd": cwd,
        }


# ============================================================
#   BACKGROUND TASK MANAGEMENT
# ============================================================

def _do_bg_status(task_id: str) -> Dict:
    """Get status and output of a background task."""
    task = BACKGROUND_TASKS.get(task_id)
    if not task:
        return {"success": False, "error": f"No background task with id '{task_id}'."}
    
    result = {
        "success": True,
        "task_id": task.task_id,
        "command": task.command,
        "status": task.status,
        "elapsed_seconds": round((task.end_time or time.time()) - task.start_time, 1),
    }
    if task.status != "running":
        result["exit_code"] = task.exit_code
        result["output"] = task.output
    else:
        result["pid"] = task.proc.pid if task.proc else None
    return result


async def _do_bg_kill(task_id: str) -> Dict:
    """Kill a running background task."""
    task = BACKGROUND_TASKS.get(task_id)
    if not task:
        return {"success": False, "error": f"No background task with id '{task_id}'."}
    if task.status != "running":
        return {"success": False, "error": f"Task '{task_id}' is already {task.status}."}
    
    _kill_proc(task.proc)
    task.status = "killed"
    task.end_time = time.time()
    task.exit_code = -9
    task.output = "[Killed by user]"
    if task._collector and not task._collector.done():
        task._collector.cancel()
    _log_terminal_exec(task.command, cwd=task.cwd, success=False, error="Killed by user")
    return {"success": True, "task_id": task_id, "status": "killed"}


def _do_bg_list() -> Dict:
    """List all background tasks."""
    tasks = []
    for t in BACKGROUND_TASKS.values():
        tasks.append({
            "task_id": t.task_id,
            "command": t.command[:80],
            "status": t.status,
            "elapsed_seconds": round((t.end_time or time.time()) - t.start_time, 1),
        })
    return {"success": True, "count": len(tasks), "tasks": tasks}
