# shell-0

An MCP server that gives a model direct, unsandboxed access to the local machine: files, a Python runtime, a Node.js runtime, and a shell. Four tools, full system access, no guardrails.

This is deliberately not sandboxed. Point it at a machine you control, and understand the blast radius before you wire it into an agent.

## Tools

- `fs` - filesystem access: read, write, edit, copy, move, mkdir, rmdir, list, tree, search, grep, stat, diff, hash, head, tail, and duplicate detection. 50 MB read cap, no file-watcher race conditions.
- `python_exec` - run Python with the full standard library and whatever is installed in the server's environment. Module-level state persists across calls.
- `js_exec` - run JavaScript in a real Node.js process. A `state` object persists across calls. Requires Node.js on PATH.
- `terminal` - run shell commands (cmd.exe on Windows, bash elsewhere), with optional background jobs you can poll and kill.

## Forensic audit (on by default)

Every filesystem change and every code or command execution is written to a rolling on-disk audit log, so nothing the tools do is silently lost:

- Writes, edits, and deletes are snapshotted with their previous contents before the change, so any overwrite or delete is recoverable.
- Reads and searches are timestamped in an access log.
- Python and shell executions are saved with their source, status, and output.

Logs live under `./data/` next to the server, split into per-session folders, and self-prune at 50 MB (oldest first). This is accountability, not sandboxing. The tools still do whatever you ask; you just get a full paper trail of it.

Move it with `FS_AUDIT_ROOT` / `EXEC_AUDIT_ROOT`, or turn it off entirely with `SHELL0_AUDIT_DISABLE=1`.

## Install

Requires Python 3.10 or newer (tested on 3.12).

```
pip install -r requirements.txt
```

For `js_exec`, install Node.js from https://nodejs.org and make sure `node` is on your PATH. The other three tools have no dependencies beyond the MCP SDK.

## Use it with an MCP client

shell-0 speaks MCP over stdio. Point your client at `server.py`. A Claude Desktop style config looks like this:

```json
{
  "mcpServers": {
    "shell-0": {
      "command": "python",
      "args": ["/absolute/path/to/shell-0/server.py"]
    }
  }
}
```

Use an absolute path to `server.py`. On Windows, if `python` is not on PATH, use the full path to `python.exe`, and either forward slashes or escaped backslashes in the paths. There is a ready-to-edit copy in `example_config.json`.

## Configuration

All optional, set as environment variables:

- `SHELL0_AUDIT_DISABLE` - set to `1` to turn the audit off (default: on).
- `FS_AUDIT_ROOT` / `EXEC_AUDIT_ROOT` - move the audit logs somewhere other than `./data`.
- `FS_AUDIT_MAX_MB` / `EXEC_AUDIT_MAX_MB` - audit size cap before pruning (default: 50).
- `OUTPUT_MAX_CHARS` - hard cap on a single tool result before it gets truncated (default: 15000).
- `PYTHON_EXEC_TIMEOUT` - `python_exec` timeout in seconds (default: 30).
- `MCP_DEBUG` - set to `true` for stderr debug logging.

## A word on safety

These tools run with your privileges and no sandbox. `terminal` and `python_exec` can do anything you can do from a shell. That is the whole point, but it means you should only connect shell-0 to agents and inputs you trust, on a machine where that access is acceptable. The audit log helps you see what happened after the fact. It does not stop anything from happening.

## License

MIT. See LICENSE.
