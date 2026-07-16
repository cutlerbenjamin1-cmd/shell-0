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
| `edit` rollback report is honest | `test_fs.py::test_edit_rollback_report_is_honest` | commit 84efe0e: a partial-fail batch rolled back atomically but still reported `changes_made:true` + an applied-looking diff + edits marked "applied". Report now matches disk truth (`written:false`, edits relabeled `rolled_back`). |
| Background timeout defaults to 600s, not 120s | `test_terminal.py::test_timeout_defaults_bg_600_fg_120` | commit 84efe0e: `timeout` defaulted to 120 and that value was passed to the bg collector, silently killing long background jobs at 120s. Now Optional -> 600 bg / 120 fg. |
| `write` with omitted content is rejected, not truncating | `test_fs.py::test_write_omitted_content_is_rejected` | 2026-07-16 audit: `content` defaulted to `""`, defeating the `is None` guard, so a forgotten payload truncated the target to empty. Default is now `None`. |
| A rejected write leaves no phantom audit snapshot | `test_fs.py::test_rejected_write_leaves_no_phantom_audit` | 2026-07-16 audit: the before-state log fired *before* the guards, recording a "write happened" snapshot for a write then rejected (syntax error / path conflict / `replace_lines` limit<=0) that never hit disk. |
| Audit size cap prunes per-write + never deletes the live session | `test_fs.py::test_audit_prune_caps_single_process_growth` | 2026-07-16 audit: the cap pruned only at the first write, so one long-lived process grew unbounded. Pruning now runs every write, at file granularity, and must never delete its own current session dir (a bug briefly introduced and caught during the fix). |
| `list(include_metadata)` survives a bad entry | `test_fs.py::test_list_metadata_survives_bad_entry` | 2026-07-16 audit: one entry whose `stat()` raised (broken symlink / Windows reparse point / OneDrive placeholder) failed the entire listing. Bad entries now report inline. |
| `_kill_proc` is async (never blocks the event loop) | `test_terminal.py::test_kill_proc_is_a_coroutine` | 2026-07-16 audit: `subprocess.run(taskkill, timeout=5)` ran inline in async code, stalling the single-threaded loop (every in-flight MCP call) up to 5s per kill. Now offloaded via `asyncio.to_thread`. |
| Background launch is audited immediately | `test_terminal.py::test_background_launch_is_audited_immediately` | 2026-07-16 audit: bg tasks were logged only at completion/kill, so a crash mid-task left no trace the task ever ran. The launch is now logged synchronously. |
| `copy`/`move` snapshot an overwritten destination | `test_fs.py::test_copy_overwrite_snapshots_destination` + `::test_move_overwrite_snapshots_destination` | 2026-07-16 audit: `shutil.copy2`/`shutil.move` silently overwrite an existing destination file with no guardrail of their own; unlike `write_file`'s overwrite mode, nothing snapshotted what was about to be clobbered. Now both do. |
| `append`/`insert` writes are audited | `test_fs.py::test_append_and_insert_are_audited` | 2026-07-16 audit: only `overwrite`/`replace_lines` were snapshotted; `append`/`insert` changed a file's contents with zero audit trail. |
| `head`/`tail` stream instead of loading the whole file | `test_fs.py::test_head_tail_stream_large_file` | 2026-07-16 audit: both called `f.readlines()` on the entire file regardless of size, defeating the point of a head/tail tool for a file too big to read in full. Now streamed - `head` stops after N lines, `tail` keeps a bounded deque. |
| Chunked `read` actually streams | `test_fs.py::test_chunked_read_streams_not_whole_file` | 2026-07-16 audit: the code comment claimed chunked (`offset`/`limit`) reads "allow larger files since we're not loading all at once", but the implementation `readlines()`'d the whole file regardless of `limit`. Now genuinely streamed. |
| `js_exec` timeout kills the whole process tree | `test_js_exec.py::test_timeout_kills_process_tree` | 2026-07-16 audit: a timeout only killed the node process itself (`proc.kill()`), leaving anything it spawned (`child_process.spawn`) orphaned. Now mirrors `terminal_exec`'s tree-kill. |
| Truncated output preserves the `error` field | `test_regression.py::test_truncate_output_preserves_error_field` | 2026-07-16 audit: the smart truncator's field list omitted `"error"`, so a failing call with an oversized string error fell straight to the metadata-only fallback envelope (which also excludes `error`) - the failure reason was silently dropped instead of trimmed. |

## Adding tests

- New behaviour -> add to the matching `test_<tool>.py`.
- Fixing a bug -> add a test tagged `@pytest.mark.regression` with a `# REGRESSION:`
  note citing what broke, and add a row to the table. If it only reproduces over
  the wire, put it in `test_regression.py` / `test_server_integration.py`.
- Assert against ground truth (the filesystem, the returned payload) rather than
  the tool's own echoed values.
