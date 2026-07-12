"""
shell-0 - an MCP server for unsandboxed local system access.

Four tools. Full system access. No guardrails:

  fs           filesystem: read/write/edit/grep/diff/hash/tree/... (50MB read cap)
  python_exec  run Python with the full stdlib + whatever is installed
  js_exec      run JavaScript in a real Node.js process
  terminal     run shell commands (cmd.exe on Windows, bash on Unix)

Every fs and exec operation is recorded to a rolling forensic audit (on by
default) so any write, edit, delete, or command stays recoverable. Point it
elsewhere with FS_AUDIT_ROOT / EXEC_AUDIT_ROOT, or turn it off with
SHELL0_AUDIT_DISABLE=1. See the README.

Transport: stdio.
"""

import sys
import os

# Make the bundled `tools` package importable when this file is run directly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import time
import asyncio
import logging
import tempfile
import traceback
from typing import Dict, List, Any, Callable

# ======================================================
#                    CONFIG
# ======================================================
DEBUG_MODE = os.getenv("MCP_DEBUG", "false").lower() == "true"

# Hard cap on serialized tool output to keep a single result from blowing up
# the caller's context. Override with OUTPUT_MAX_CHARS.
OUTPUT_MAX_CHARS = int(os.getenv("OUTPUT_MAX_CHARS", "15000"))

# Keep third-party libraries from scribbling on stderr (stdio transport shares it).
for _name in ("httpx", "httpcore"):
    _l = logging.getLogger(_name)
    _l.handlers.clear()
    _l.setLevel(logging.ERROR)
    _l.propagate = False

# Windows: line-buffer stdout so JSON-RPC frames flush cleanly.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def log(msg: str):
    """Stderr logging, gated behind MCP_DEBUG."""
    if DEBUG_MODE:
        sys.stderr.write(f"[shell-0] {msg}\n")
        sys.stderr.flush()


# ======================================================
#                    MCP IMPORTS
# ======================================================
from mcp.server import Server, InitializationOptions, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# SDK cancellation race fix (upstream #2416): cancel() and respond() race across
# tasks on the _completed flag, and the assert in respond() can crash the server
# when a CancelledNotification lands after the handler already finished. Patch it
# here so it survives pip upgrades instead of editing site-packages.
from mcp.shared.session import RequestResponder as _RP

_original_respond = _RP.respond


async def _safe_respond(self, response):
    if self._completed:
        log(f"respond() skipped for request {self.request_id} - already completed (race with cancel)")
        return
    return await _original_respond(self, response)


_RP.respond = _safe_respond

_original_cancel = _RP.cancel


async def _safe_cancel(self):
    if hasattr(self, "_cancel_scope"):
        self._cancel_scope.cancel()
    if self._completed:
        log(f"cancel() skipped for request {self.request_id} - already responded")
        return
    return await _original_cancel(self)


_RP.cancel = _safe_cancel


# ======================================================
#                    TOOL IMPORTS
# ======================================================
import tools.filesystem as filesystem
import tools.python_runner as python_runner
import tools.js_runner as js_runner
import tools.terminal_exec as terminal_exec


# ======================================================
#                    TOOL REGISTRY
# ======================================================
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, func: Callable, description: str, input_schema: Dict[str, Any]) -> None:
        self._tools[name] = {
            "function": func,
            "schema": Tool(name=name, description=description, inputSchema=input_schema),
        }

    def get_schemas(self) -> List[Tool]:
        return [t["schema"] for t in self._tools.values()]

    def get_dispatch_map(self) -> Dict[str, Callable]:
        return {name: t["function"] for name, t in self._tools.items()}

    def keys(self) -> List[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


_TOOL_REGISTRY = ToolRegistry()


# ======================================================
#                    REGISTRATIONS
# ======================================================
_TOOL_REGISTRY.register(
    name="python_exec",
    func=python_runner.exec_python,
    description=(
        "Execute Python code with full system access. Returns {success, output, result, error}. "
        "Use for: data processing, file operations, API calls, system automation, any Python task. "
        "Prefer over js_exec unless JS-specific features needed. "
        "UNLOCKED: all imports (os, subprocess, requests, pathlib), file I/O, network access. "
        "Persistent state via module-level variables across calls. "
        "Limits: 30s timeout (PYTHON_EXEC_TIMEOUT env), 100k chars, 2k lines, 20k AST nodes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code. Use print() for output. Last expression value returned in 'result'."
            },
            "extended_imports": {
                "type": "boolean",
                "description": "Pre-import numpy/pandas if available. Default: false."
            }
        },
        "required": ["code"]
    }
)

_TOOL_REGISTRY.register(
    name="fs",
    func=filesystem.fs,
    description=(
        "Full shell filesystem access (UNSANDBOXED, 50MB read limit). Use for all file operations. "
        "Doesn't encounter race conditions with file watchers. "
        "grep contains all the functionality of bash + powershell filesystem search. "
        "Auto-fixes code fences and smart quotes on write. "
        "Actions: read, write, edit, delete, copy, move, mkdir, rmdir, list, tree, search, grep, stat, diff, hash, touch, head, tail, duplicates. "
        "edit: [{old_text, new_text}]. grep: regex search in directories. "
        "diff: compare files/strings. hash: SHA256/MD5 checksums. duplicates: find duplicate files by hash."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "edit", "delete", "copy", "move", "mkdir", "rmdir", "list", "tree", "search", "grep", "stat", "diff", "hash", "touch", "head", "tail", "duplicates"]},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "destination": {"type": "string"},
            "pattern": {"type": "string"},
            "recursive": {"type": "boolean", "description": "For rmdir: recurse into non-empty directories."},
            "confirm": {"type": "boolean", "description": "For rmdir: confirm deleting a directory that contains more than 50 items."},
            "include_metadata": {"type": "boolean", "description": "For list: include size, type, and mtime for each entry."},
            "offset": {"type": "number", "description": "Starting line (1-based) for read chunk or write insert/replace"},
            "limit": {"type": "number", "description": "Lines to read (0=all) or lines to replace in replace_lines mode"},
            "mode": {"type": "string", "enum": ["overwrite", "append", "insert", "replace_lines"], "description": "Write mode (default: overwrite)"},
            "edits": {"type": "array", "description": "For edit: [{old_text, new_text}, ...]", "items": {"type": "object"}},
            "dry_run": {"type": "boolean", "description": "For edit: preview diff without writing"},
            "preserve_indentation": {"type": "boolean", "description": "For edit: apply old_text's leading indentation to new_text (default: true)."},
            "max_depth": {"type": "number", "description": "For grep: max directory depth"},
            "max_results": {"type": "number", "description": "For grep: max matches"},
            "file_pattern": {"type": "string", "description": "For grep: file glob (*.py)"},
            "ignore_case": {"type": "boolean", "description": "For grep: case-insensitive search (default: false)"},
            "regex": {"type": "boolean", "description": "For grep: treat pattern as regex instead of substring (default: false)"},
            "context_lines": {"type": "number", "description": "For grep/diff: lines of context (default: 0/3)"},
            "max_line_length": {"type": "number", "description": "For grep: max chars per line in output (default: 500)"},
            "path_a": {"type": "string", "description": "For diff: first file path"},
            "path_b": {"type": "string", "description": "For diff: second file path"},
            "content_a": {"type": "string", "description": "For diff: first content string"},
            "content_b": {"type": "string", "description": "For diff: second content string"},
            "output_format": {"type": "string", "enum": ["unified", "context", "ndiff"], "description": "For diff: output format (default: unified)"},
            "algorithms": {"type": "array", "items": {"type": "string"}, "description": "For hash: list of algorithms (md5, sha1, sha256, sha512)"},
            "create_parents": {"type": "boolean", "description": "For touch: create parent dirs (default: true)"},
            "lines": {"type": "number", "description": "For head/tail: number of lines (default: 10)"},
            "algorithm": {"type": "string", "description": "For duplicates: hash algorithm (default: sha256)"},
            "min_size": {"type": "number", "description": "For duplicates: min file size in bytes (default: 1)"}
        },
        "required": ["action"]
    }
)

_TOOL_REGISTRY.register(
    name="terminal",
    func=terminal_exec.terminal_exec,
    description=(
        "Execute shell commands. Returns {success, output, exit_code, duration_seconds}. "
        "Use for: git, npm, pip, system commands, anything requiring shell. "
        "UNRESTRICTED - full privileges, no sandboxing. "
        "Windows: cmd.exe, Unix: bash. Timeout: 120s default, 600s max. "
        "Background: run_in_background=true returns task_id. Use bg_status/bg_kill/bg_list to manage. "
        "For file ops prefer fs; for Python prefer python_exec."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute. Supports pipes, redirects, chaining (&&, ||)."
            },
            "cwd": {"type": "string", "description": "Working directory. Default: current dir."},
            "timeout": {"type": "number", "description": "Timeout in seconds. Default: 120. Max: 600."},
            "run_in_background": {"type": "boolean", "description": "Run command in background, return task_id immediately. Default: false."},
            "bg_status": {"type": "string", "description": "Get status/output of a background task by task_id."},
            "bg_kill": {"type": "string", "description": "Kill a running background task by task_id."},
            "bg_list": {"type": "boolean", "description": "List all background tasks with status. Set true."},
            "description": {"type": "string", "description": "Brief description for logging (e.g., 'Install deps')."}
        }
    }
)

_TOOL_REGISTRY.register(
    name="js_exec",
    func=js_runner.exec_js,
    description=(
        "Execute JavaScript code in a full Node.js environment. "
        "Returns an object with keys: success (bool), output (combined stdout/stderr), "
        "stdout, stderr, exitCode, and outputTruncated (bool if output exceeded 100KB). "
        "Use this tool when: (1) manipulating JSON data where JavaScript's native "
        "handling is cleaner than Python, (2) testing algorithms where JS syntax "
        "is preferred, (3) using npm packages not available in Python, (4) working "
        "with Node.js-specific APIs. "
        "Do NOT use this tool when: (1) generating visualizations - Node.js has no "
        "DOM, canvas, or browser APIs (use python_exec with matplotlib instead), "
        "(2) the task is equally achievable in Python (prefer python_exec for consistency). "
        "The tool provides persistent state across calls via a `state` object that "
        "is automatically loaded from and saved to .shell0_js_state.json in the working "
        "directory. Assign values to `state.myKey` to persist them between invocations. "
        "Limitations: 30-second default timeout (configurable up to 600s), 100K character "
        "source limit, 2000 line limit, 100KB output cap. On timeout the process is killed. "
        "Syntax errors return in stderr with line numbers. Requires Node.js on PATH."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "JavaScript source code to execute. Supports require(), async/await, "
                    "and ES6+ syntax. Use console.log() for output. Access persistent state "
                    "via the `state` object (auto-loaded/saved). Example: "
                    "`const data = require('fs').readFileSync('file.json'); console.log(JSON.parse(data));`"
                )
            },
            "timeout": {
                "type": "number",
                "description": "Execution timeout in seconds. Default: 30. Maximum: 600."
            }
        },
        "required": ["code"]
    }
)

TOOL_REGISTRY = _TOOL_REGISTRY.get_dispatch_map()
log(f"Registered {len(TOOL_REGISTRY)} tools: {list(TOOL_REGISTRY.keys())}")


# ======================================================
#                    OUTPUT HYGIENE
# ======================================================
# Tools whose outputs bypass the OUTPUT_MAX_CHARS cap. Use sparingly.
TRUNCATION_EXEMPT_TOOLS = set()


def _truncate_content_field(content: str, max_chars: int) -> tuple:
    """
    Truncate a content/output string, keeping head and tail for context.
    Returns (truncated_content, was_truncated, trunc_info).
    """
    if len(content) <= max_chars:
        return content, False, None

    total = len(content)
    head_size = int(max_chars * 0.6)
    tail_size = max_chars - head_size - 150  # Room for marker with offset info

    head = content[:head_size]
    tail = content[-tail_size:] if tail_size > 0 else ""

    tail_start = total - tail_size if tail_size > 0 else total
    omitted = total - head_size - (tail_size if tail_size > 0 else 0)
    marker = (
        f"\n\n... [{omitted:,} chars omitted | "
        f"showing 0:{head_size:,} + {tail_start:,}:{total:,} | "
        f"resume offset {head_size}] ...\n\n"
    )

    trunc_info = {
        "head_end": head_size,
        "tail_start": tail_start,
        "total_chars": total,
        "chars_omitted": omitted,
    }
    return head + marker + tail, True, trunc_info


def _sanitize_surrogates(obj):
    """
    Recursively replace surrogate characters (U+D800-U+DFFF) that can't be
    encoded to UTF-8. These cause PydanticSerializationError when MCP serializes.
    """
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    elif isinstance(obj, dict):
        return {k: _sanitize_surrogates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_surrogates(item) for item in obj]
    return obj


def _truncate_output(payload: dict, tool_name: str = "") -> dict:
    """
    Truncate output if serialized JSON exceeds OUTPUT_MAX_CHARS, preserving valid
    JSON. Prefers trimming large 'content'/'output' fields; falls back to an
    aggressive metadata-only envelope. Tools in TRUNCATION_EXEMPT_TOOLS bypass.
    """
    if tool_name in TRUNCATION_EXEMPT_TOOLS:
        return payload

    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= OUTPUT_MAX_CHARS:
        return payload

    actual_len = len(text)
    log(f"[WARN] Output truncated: {actual_len} -> ~{OUTPUT_MAX_CHARS} chars")

    content_fields = ["content", "output", "result", "data", "text"]
    max_field_chars = OUTPUT_MAX_CHARS - 500  # Leave room for metadata

    for field in content_fields:
        if field in payload and isinstance(payload[field], str):
            field_len = len(payload[field])
            if field_len > max_field_chars:
                truncated_content, was_truncated, trunc_info = _truncate_content_field(
                    payload[field], max_field_chars
                )
                if was_truncated:
                    payload = payload.copy()
                    payload[field] = truncated_content
                    payload["_truncated_field"] = field
                    payload["_original_field_size"] = field_len
                    if trunc_info:
                        payload["_truncation_resume_offset"] = trunc_info["head_end"]
                        payload["_truncation_gap"] = f"{trunc_info['head_end']}-{trunc_info['tail_start']}"

                    new_text = json.dumps(payload, ensure_ascii=False)
                    if len(new_text) <= OUTPUT_MAX_CHARS:
                        return payload

    # Fallback: aggressive truncation to a metadata-only envelope.
    truncated_payload = {
        "success": payload.get("success", True),
        "truncated": True,
        "original_size": actual_len,
        "max_size": OUTPUT_MAX_CHARS,
    }
    for key in ["path", "action", "mode", "exit_code", "total_lines", "size", "query", "count"]:
        if key in payload:
            truncated_payload[key] = payload[key]

    truncated_payload["note"] = "Response too large. Use offset/limit for chunked reads."
    envelope_size = len(json.dumps(truncated_payload, ensure_ascii=False))
    available_chars = OUTPUT_MAX_CHARS - envelope_size - 100

    if available_chars > 500:
        for field in content_fields:
            if field in payload and isinstance(payload[field], str):
                preview, _, _ = _truncate_content_field(payload[field], available_chars)
                truncated_payload["preview"] = preview
                break

    return truncated_payload


# ======================================================
#                    MCP SERVER
# ======================================================
server = Server("shell-0")


@server.list_tools()
async def list_tools():
    return _TOOL_REGISTRY.get_schemas()


@server.call_tool()
async def call_tool(name: str, args: dict):
    if name not in TOOL_REGISTRY:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Unknown tool: {name}"}))]

    log(f"Running: {name} args={args}")
    start = time.time()
    try:
        result = await TOOL_REGISTRY[name](**args)
    except Exception as e:
        log(f"ERROR in {name}: {e}")
        return [TextContent(type="text", text=json.dumps({"success": False, "error": str(e)}))]

    log(f"Completed: {name} in {time.time() - start:.3f}s")
    payload = result if isinstance(result, dict) else {"success": True, "result": result}
    payload = _truncate_output(payload, tool_name=name)
    payload = _sanitize_surrogates(payload)
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


# ======================================================
#                    MAIN
# ======================================================
async def main():
    capabilities = server.get_capabilities(NotificationOptions(), {})
    init = InitializationOptions(server_name="shell-0", server_version="0.1.0", capabilities=capabilities)
    log("Starting stdio server...")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Shutting down...")
    except Exception as e:
        err_path = os.path.join(tempfile.gettempdir(), "shell0_mcp_error.log")
        with open(err_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {e}\n")
            f.write(traceback.format_exc() + "\n")
        sys.exit(1)
