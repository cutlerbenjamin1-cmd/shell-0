# shell-0

An MCP server that gives a model direct, unsandboxed access to the local machine: files, a Python runtime, a Node.js runtime, and a shell. Four tools, full system access, no guardrails.

This is deliberately not sandboxed. Point it at a machine you control, and understand the blast radius before you wire it into an agent.

## Tools

- `fs` - filesystem access: read, write, edit, copy, move, mkdir, rmdir, list, tree, search, grep, stat, diff, hash, touch, head, tail, and duplicate detection. 50 MB read cap (`FS_MAX_READ_BYTES`), no file-watcher race conditions. Writes get repair-only-if-broken autofixing: markdown code fences and smart quotes are stripped/normalized, and a JSON or Python write that wouldn't parse is rejected instead of silently corrupting the file.
- `python_exec` - run Python with the full standard library and whatever is installed in the server's environment. Code runs in a persistent worker subprocess, so module-level state survives across calls and a timeout is a real `SIGKILL` of the worker, not a best-effort nudge - the next call just gets a fresh one. Same fence/quote autofixing as `fs`.
- `js_exec` - run JavaScript in a real Node.js process. A `state` object persists across calls, loaded from and saved to a JSON file on disk; concurrent calls are serialized so they can't race that file. Requires Node.js on PATH.
- `terminal` - run shell commands (cmd.exe on Windows, bash elsewhere), with optional background jobs you can poll and kill. A timeout kills the whole process tree it spawned, not just the top-level command.

## Forensic audit (on by default)

Every filesystem change and every code or command execution is written to a rolling on-disk audit log, so nothing the tools do is silently lost:

- Every write mode (overwrite, append, insert, replace_lines), edit, copy, move, and delete is snapshotted with its previous contents before the change - including whatever an overwriting `copy`/`move` is about to clobber at the destination - so any of them is recoverable.
- Reads, greps, filename searches, and touches are timestamped in an access log.
- Python, JavaScript, and shell executions are saved with their source, status, and output.

Logs live under `./data/` next to the server, split into per-session folders, and self-prune at 50 MB (oldest first, checked on every write - not just at startup). This is accountability, not sandboxing. The tools still do whatever you ask; you just get a full paper trail of it. The one exception: `rmdir` refuses to delete drive roots, UNC roots, or your home directory outright, with no override - the only hardcoded guardrail in an otherwise unrestricted tool.

Move it with `FS_AUDIT_ROOT` / `EXEC_AUDIT_ROOT`, or turn it off entirely with `SHELL0_AUDIT_DISABLE=1`. Each tool module (`tools/filesystem.py`, `tools/python_runner.py`, `tools/js_runner.py`, `tools/terminal_exec.py`) carries its own copy of this audit logic rather than importing a shared one - deliberately, so any single tool file can be lifted out and dropped into another project with nothing else to bring along.

## Install

Requires Python 3.10 or newer (tested on 3.12).

Install as a package to get a `shell-0` command on your PATH (recommended - an isolated installer keeps it off your system Python):

```
pipx install git+https://github.com/cutlerbenjamin1-cmd/shell-0
# or run it ad-hoc without a checkout:
uvx --from git+https://github.com/cutlerbenjamin1-cmd/shell-0 shell-0
```

From a local checkout: `pip install .`

Or skip packaging entirely, install the one dependency, and run `server.py` directly:

```
pip install -r requirements.txt
```

For `js_exec`, install Node.js from https://nodejs.org and make sure `node` is on your PATH. `python_exec(extended_imports=true)` can pre-import numpy/pandas if you add the extra: `pip install "shell-0[extended]"`.

## Use it with an MCP client

shell-0 speaks MCP over stdio. If you installed the package, point your client at the `shell-0` command:

```json
{
  "mcpServers": {
    "shell-0": {
      "command": "shell-0"
    }
  }
}
```

If you did not install it as a package, point `python` at `server.py` with an absolute path instead:

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

On Windows, if the command is not found, use the full path to the installed `shell-0.exe` (in your pipx/venv Scripts directory) or to `python.exe`, with forward slashes or escaped backslashes. There is a ready-to-edit copy in `example_config.json`.

## Running over HTTP (optional)

shell-0 speaks stdio. If your MCP client wants HTTP instead (llama.cpp's web UI, OpenWebUI, and similar), there is a companion bridge that serves shell-0 over MCP streamable HTTP: [mcp-http-bridge](https://github.com/cutlerbenjamin1-cmd/mcp-http-bridge). Run it from this directory and point your client at `http://127.0.0.1:8818/mcp`.

> **WARNING: do not expose shell-0's tools over the network without thinking hard first.**
>
> shell-0's tools (`terminal`, `python_exec`, `js_exec`, `fs`) run unsandboxed with your full privileges. Serving them over HTTP on anything other than `127.0.0.1` hands remote code execution to anyone who can reach the port - and even on `127.0.0.1`, a web page in any browser tab can reach a loopback port, which is why the bridge validates the `Origin` header. The bridge ships a filter that disables every execution tool **and `fs`** by default (arbitrary file write is RCE-equivalent), rejects unknown browser origins, and supports a shared-secret header. Leave those defaults unless you have put real authentication and TLS in front of it, and even then only enable what you actually need.

## Configuration

All optional, set as environment variables:

- `SHELL0_AUDIT_DISABLE` - set to `1` to turn the audit off (default: on).
- `FS_AUDIT_ROOT` / `EXEC_AUDIT_ROOT` - move the audit logs somewhere other than `./data`.
- `FS_AUDIT_MAX_MB` / `EXEC_AUDIT_MAX_MB` - audit size cap before pruning (default: 50).
- `FS_MAX_READ_BYTES` - full-file read cap in bytes for `fs(action="read")` with no `limit` (default: 50MB). Chunked reads (`offset`/`limit`) stream instead of loading the whole file, so they aren't subject to this cap.
- `FS_MAX_TREE_DEPTH` - max directory depth for `tree`, `search`, and `grep` (default: 15).
- `OUTPUT_MAX_CHARS` - hard cap on a single tool result before it gets truncated (default: 15000).
- `PYTHON_EXEC_TIMEOUT` - `python_exec` timeout in seconds (default: 30).
- `PYTHON_EXEC_MAX_MEMORY` - `python_exec` worker address-space cap in bytes (default: 8GB). POSIX only (RLIMIT_AS); no-op on Windows, which has no equivalent cheap hard cap.
- `PYTHON_EXEC_MAX_OUTPUT` - max captured stdout/stderr chars per `python_exec` call before clipping (default: 1,000,000).
- `JS_EXEC_TIMEOUT` - `js_exec` timeout in seconds (default: 30).
- `MCP_DEBUG` - set to `true` for stderr debug logging.

## A word on safety

These tools run with your privileges and no sandbox. `terminal` and `python_exec` can do anything you can do from a shell. That is the whole point, but it means you should only connect shell-0 to agents and inputs you trust, on a machine where that access is acceptable. The audit log helps you see what happened after the fact. It does not stop anything from happening.

## License

MIT. See LICENSE.

## Testing

shell-0 has a pytest suite (coverage + regression + a live-stdio smoke layer) and
an interactive driver. Full details, including the regression provenance table,
are in [`tests/README.md`](tests/README.md).

```bash
pip install -e ".[test]"     # test deps, into a venv

pytest                       # full sweep
pytest -m regression         # only the guards for bugs actually hit
python manual.py             # drive the real server by hand over stdio
```

The suite is hermetic: every test runs in a temp dir with the forensic audit
redirected, and never touches paths outside it. The `js_exec` tests skip
automatically when Node.js isn't on `PATH`.
