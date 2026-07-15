# shell-0 test suite

Regression + coverage tests for the four tools (`fs`, `python_exec`, `js_exec`,
`terminal`) and the MCP server that wraps them. shell-0 is unsandboxed and shipped
publicly - the tools have real blast radius - so their behaviour needs to be
pinned down and kept pinned.

## Running

```bash
# from the repo root, in a venv
pip install -e ".[test]"

pytest                    # full automated sweep
pytest -m regression      # only the named regressions (bugs we have actually hit)
pytest tests/test_fs.py   # a single tool
pytest -k edit            # by name

python manual.py          # interactive: drive the real server over stdio by hand
python manual.py --audit-tmp   # ...with the forensic audit redirected to a temp dir
```

`js_exec` tests skip automatically when Node.js isn't on `PATH`.

## The three layers

1. **Coverage** - every action and mode of every tool, happy path + edges + error
   paths, called in-process (`await tools.filesystem.fs(...)`). Fast and precise.
2. **Regression** (`-m regression`) - one test per bug we have actually hit, each
   tagged `@pytest.mark.regression` beside a `# REGRESSION:` note citing the
   incident. See the table below.
3. **Integration** (`test_server_integration.py`, parts of `test_regression.py`) -
   drives the real `server.py` over live MCP stdio through `stdio_driver.py`,
   exercising what the in-process layer skips: schema registration, dispatch,
   output truncation, surrogate scrubbing, JSON-RPC framing. `manual.py` uses the
   same driver, so hand-testing and the smoke tests hit the identical surface.

## Hermetic by design

The tools can write anywhere and run anything, so the suite fences *itself* in
(see `conftest.py`):

- every test runs under a per-test `tmp_path`; nothing touches real paths;
- the forensic audit is redirected into `tmp_path` and **off by default** - left
  on, `grep`/`search` would match the query text that audit logs into
  `access.log` and contaminate their own results. Audit-behaviour tests opt back
  in with the `audit_on` fixture;
- `js_exec` writes its state file and temp scripts into `cwd`, so js tests run
  from a throwaway dir (`workdir` fixture);
- the persistent python worker and the module-level `asyncio.Lock`s are recreated
  per test - pytest-asyncio uses a fresh event loop per test, and a lock or
  subprocess bound to a closed loop would otherwise error on Windows;
- terminal tests use only boring commands (`echo`, a `python -c` one-liner, a
  controlled sleeper) - never anything destructive.

## Regression provenance

Each guard maps to a real incident. Don't delete one because it "looks trivial" -
read the incident first.

| Guard | Test | Incident |
|-------|------|----------|
| No-output terminal command returns fast (doesn't hang) | `test_terminal.py::test_no_output_command_returns_quickly` + `test_regression.py::test_no_output_command_returns_over_stdio` | 2025-11-30: the child inherited the MCP stdin (the JSON-RPC channel) and hung forever; fixed with `stdin=DEVNULL`. Only fully reproduces over stdio. |
| `python -c` on success returns | `test_terminal.py::test_python_dash_c_returns` | Same bug - this was the canonical repro. |
| Timeout returns cleanly + kills the process tree | `test_terminal.py::test_timeout_returns_clean` | 2025-12-21 audit: `proc.kill` killed only the shell, not its children. Now `taskkill /T /F`. |
| Sequential multi-edit stays clean | `test_fs.py::test_edit_multiple_sequential` | 2025-11-30: multiple edits duplicated / overwrote code (off-by-one). |
| A failing edit in a batch writes nothing | `test_fs.py::test_edit_all_or_nothing_on_failure` | Same era: half-applied edits left broken syntax. `edit` is now all-or-nothing + atomic. |
| `replace_lines` with `limit=0` errors | `test_fs.py::test_replace_lines_requires_positive_limit` | 2025-12-08: `limit=0` silently acted as insert and corrupted files. |
| Overwrite is atomic (no temp litter) | `test_fs.py::test_overwrite_atomic_no_tmp_litter` | Audit: non-atomic overwrite risked corruption on crash. Now `mkstemp` + `os.replace`. |
| `rmdir` refuses drive roots / home | `test_fs.py::test_rmdir_refuses_danger_path` | Audit: the tool's only guardrail once covered `C:` only; now all drive roots + home. |
| Autofix keeps valid code verbatim | `test_fs.py::test_autofix_preserves_inline_backticks` | Repair-only-if-broken: a ``` sequence inside a string literal must not be stripped. |
| Python state persists; timeout resets it | `test_python_exec.py::test_persistent_state_across_calls` + `::test_timeout_kills_and_resets_state` | Worker design: reused for state, really killed (not nudged) on timeout. |
| Python worker crash recovers | `test_python_exec.py::test_worker_crash_recovers` | Hard exit (`os._exit` / OOM) must report cleanly and respawn a fresh worker. |
| Raw fd-1 writes don't corrupt the protocol | `test_python_exec.py::test_fd1_write_does_not_corrupt_protocol` | Worker reserves fd 1 for the newline-JSON protocol and redirects user fd-1 writes to fd 2. |
| Concurrent `js_exec` doesn't corrupt state | `test_js_exec.py::test_concurrent_state_no_corruption` | 2026-07-13: concurrent calls raced the shared on-disk state file; an `asyncio.Lock` now serializes them. |
| `js_exec` writes an audit record | `test_js_exec.py::test_audit_written_on_success_and_error` | 2026-07-13: js_exec had zero audit logging while python/terminal were audited. |
| Lone surrogates don't crash serialization | `test_regression.py::test_sanitize_surrogates_survives_lone_surrogate` | `_sanitize_surrogates` used `surrogateescape`, which *itself* raised on a lone surrogate - **caught and fixed while writing this suite**. |
| Oversized output stays valid JSON | `test_regression.py::test_truncate_output_keeps_valid_json_envelope` + `test_server_integration.py::test_output_truncation_over_stdio` | The server caps serialized output; it must trim to a valid envelope, not break framing. |

## Adding tests

- New behaviour -> add to the matching `test_<tool>.py`.
- Fixing a bug -> add a test tagged `@pytest.mark.regression` with a `# REGRESSION:`
  note citing what broke, and add a row to the table. If it only reproduces over
  the wire, put it in `test_regression.py` / `test_server_integration.py`.
- Assert against ground truth (the filesystem, the returned payload) rather than
  the tool's own echoed values.
