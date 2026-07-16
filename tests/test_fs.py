"""Coverage + regression tests for the fs tool (tools/filesystem.py).

Every test verifies against the filesystem itself (the ground truth) wherever a
side effect exists, and against the returned payload for read-only actions.
"""
import hashlib
from pathlib import Path

import pytest

import tools.filesystem as fsmod


# ------------------------------- read / write -------------------------------

async def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "a.txt"
    r = await fsmod.fs(action="write", path=str(p), content="hello world\n")
    assert r["success"] is True
    r = await fsmod.fs(action="read", path=str(p))
    assert r["success"] is True
    assert r["content"] == "hello world\n"
    assert r["total_lines"] == 1


async def test_read_missing_file(tmp_path):
    r = await fsmod.fs(action="read", path=str(tmp_path / "nope.txt"))
    assert r["success"] is False
    assert "not found" in r["error"].lower()


async def test_read_offset_limit(tmp_path):
    p = tmp_path / "multi.txt"
    p.write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    r = await fsmod.fs(action="read", path=str(p), offset=2, limit=2)
    assert r["success"] is True
    assert r["content"] == "l2\nl3\n"
    assert r["offset"] == 2
    assert r["lines_returned"] == 2
    assert r["has_more"] is True


async def test_write_append(tmp_path):
    p = tmp_path / "b.txt"
    await fsmod.fs(action="write", path=str(p), content="one\n")
    await fsmod.fs(action="write", path=str(p), content="two\n", mode="append")
    assert p.read_text(encoding="utf-8") == "one\ntwo\n"


async def test_write_insert(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    await fsmod.fs(action="write", path=str(p), content="X\n", mode="insert", offset=2)
    assert p.read_text(encoding="utf-8") == "a\nX\nb\nc\n"


async def test_write_replace_lines(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    r = await fsmod.fs(action="write", path=str(p), content="B\n",
                       mode="replace_lines", offset=2, limit=1)
    assert r["success"] is True
    assert p.read_text(encoding="utf-8") == "a\nB\nc\n"


@pytest.mark.regression
async def test_replace_lines_requires_positive_limit(tmp_path):
    # REGRESSION: replace_lines + limit=0 once silently acted as insert; it must
    # now hard-error rather than corrupt the file. (session 624fdb76, 2025-12-08)
    p = tmp_path / "e.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    r = await fsmod.fs(action="write", path=str(p), content="X\n",
                       mode="replace_lines", offset=1, limit=0)
    assert r["success"] is False
    assert "limit > 0" in r["error"]
    assert p.read_text(encoding="utf-8") == "a\nb\nc\n"  # untouched


@pytest.mark.regression
async def test_overwrite_atomic_no_tmp_litter(tmp_path):
    # REGRESSION: overwrite must be atomic (temp + os.replace) and leave no
    # partial/temp files behind. (audit 8df5b5e1: non-atomic overwrite = corruption)
    p = tmp_path / "atomic.txt"
    await fsmod.fs(action="write", path=str(p), content="v1\n")
    await fsmod.fs(action="write", path=str(p), content="v2 longer content\n")
    assert p.read_text(encoding="utf-8") == "v2 longer content\n"
    leftovers = [f.name for f in tmp_path.iterdir() if f.is_file() and f.name != "atomic.txt"]
    assert leftovers == [], f"unexpected temp litter: {leftovers}"


# ---------------------------------- edit ------------------------------------

async def test_edit_single(tmp_path):
    p = tmp_path / "s.py"
    p.write_text("x = 1\ny = 2\n", encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p),
                       edits=[{"old_text": "x = 1", "new_text": "x = 42"}])
    assert r["success"] is True
    assert r["edits_applied"] == 1
    assert p.read_text(encoding="utf-8") == "x = 42\ny = 2\n"


@pytest.mark.regression
async def test_edit_multiple_sequential(tmp_path):
    # REGRESSION: multiple edits once duplicated / overwrote code (off-by-1).
    # They must apply cleanly in sequence. (session 4c9451c7, 2025-11-30)
    p = tmp_path / "m.py"
    p.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p), edits=[
        {"old_text": "a = 1", "new_text": "a = 10"},
        {"old_text": "c = 3", "new_text": "c = 30"},
    ])
    assert r["success"] is True
    assert r["edits_applied"] == 2
    assert p.read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 30\n"


@pytest.mark.regression
async def test_edit_all_or_nothing_on_failure(tmp_path):
    # REGRESSION: one failing edit in a batch must leave the file byte-for-byte
    # untouched (atomic all-or-nothing), never half-applied. (session 4c9451c7)
    p = tmp_path / "f.py"
    original = "a = 1\nb = 2\n"
    p.write_text(original, encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p), edits=[
        {"old_text": "a = 1", "new_text": "a = 10"},
        {"old_text": "NOPE_not_present", "new_text": "whatever"},
    ])
    assert r["success"] is False
    assert r["edits_failed"] == 1
    assert p.read_text(encoding="utf-8") == original


async def test_edit_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "dr.py"
    p.write_text("x = 1\n", encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p),
                       edits=[{"old_text": "x = 1", "new_text": "x = 2"}], dry_run=True)
    assert r["success"] is True
    assert r["dry_run"] is True
    assert r["changes_made"] is True
    assert r["diff"]
    assert p.read_text(encoding="utf-8") == "x = 1\n"


async def test_edit_text_not_found(tmp_path):
    p = tmp_path / "nf.py"
    p.write_text("x = 1\n", encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p),
                       edits=[{"old_text": "zzz", "new_text": "q"}])
    assert r["success"] is False
    assert r["edits_failed"] == 1
    assert p.read_text(encoding="utf-8") == "x = 1\n"


async def test_edit_first_occurrence_only(tmp_path):
    p = tmp_path / "dup.txt"
    p.write_text("dup\ndup\n", encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p),
                       edits=[{"old_text": "dup", "new_text": "X"}])
    assert r["success"] is True
    assert p.read_text(encoding="utf-8") == "X\ndup\n"


async def test_edit_missing_file(tmp_path):
    r = await fsmod.fs(action="edit", path=str(tmp_path / "ghost.txt"),
                       edits=[{"old_text": "a", "new_text": "b"}])
    assert r["success"] is False
    assert "not found" in r["error"].lower()


async def test_edit_preserve_indentation(tmp_path):
    p = tmp_path / "ind.py"
    p.write_text("    x = 1\n", encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p),
                       edits=[{"old_text": "    x = 1", "new_text": "x = 2"}],
                       preserve_indentation=True)
    assert r["success"] is True
    assert p.read_text(encoding="utf-8") == "    x = 2\n"


# ----------------------- delete / copy / move / dirs ------------------------

async def test_delete_file(tmp_path):
    p = tmp_path / "del.txt"
    p.write_text("bye\n", encoding="utf-8")
    r = await fsmod.fs(action="delete", path=str(p))
    assert r["success"] is True
    assert not p.exists()


async def test_copy_file(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("data\n", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    r = await fsmod.fs(action="copy", path=str(src), destination=str(dst))
    assert r["success"] is True
    assert dst.read_text(encoding="utf-8") == "data\n"
    assert src.exists()


async def test_move_file(tmp_path):
    src = tmp_path / "mv.txt"
    src.write_text("x\n", encoding="utf-8")
    dst = tmp_path / "moved.txt"
    r = await fsmod.fs(action="move", path=str(src), destination=str(dst))
    assert r["success"] is True
    assert dst.exists() and not src.exists()


async def test_mkdir_and_rmdir(tmp_path):
    d = tmp_path / "newdir"
    r = await fsmod.fs(action="mkdir", path=str(d))
    assert r["success"] is True and d.is_dir()
    r = await fsmod.fs(action="rmdir", path=str(d))
    assert r["success"] is True and not d.exists()


@pytest.mark.regression
async def test_rmdir_refuses_danger_path():
    # REGRESSION: rmdir must refuse drive roots / home - the only guardrail in an
    # otherwise unsandboxed tool. (audit 8df5b5e1: guard once only covered C:.)
    # Home is guaranteed to be in the danger list; the refusal happens before any
    # deletion, and a non-recursive rmdir of a non-empty dir would fail anyway.
    r = await fsmod.fs(action="rmdir", path=str(Path.home()))
    assert r["success"] is False


# --------------------------- list / tree / search ---------------------------

async def test_list_directory(tmp_path):
    (tmp_path / "f1.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    r = await fsmod.fs(action="list", path=str(tmp_path))
    assert r["success"] is True
    assert "f1.txt" in r["items"] and "sub" in r["items"]


async def test_list_directory_metadata(tmp_path):
    (tmp_path / "f1.txt").write_text("abc", encoding="utf-8")
    r = await fsmod.fs(action="list", path=str(tmp_path), include_metadata=True)
    assert r["success"] is True
    entry = next(e for e in r["items"] if e["name"] == "f1.txt")
    assert entry["type"] == "file"
    assert entry["size"] == 3


async def test_tree(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "leaf.txt").write_text("x", encoding="utf-8")
    r = await fsmod.fs(action="tree", path=str(tmp_path))
    assert r["success"] is True
    assert "sub" in r["tree"][""]["directories"]
    assert "leaf.txt" in r["tree"]["sub"]["files"]


async def test_search_files_glob(tmp_path):
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("x", encoding="utf-8")
    r = await fsmod.fs(action="search", path=str(tmp_path), pattern="*.py")
    assert r["success"] is True
    names = [Path(m).name for m in r["matches"]]
    assert "keep.py" in names and "skip.txt" not in names


# ----------------------------------- grep -----------------------------------

async def test_grep_substring(tmp_path):
    (tmp_path / "g.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    r = await fsmod.fs(action="grep", path=str(tmp_path), pattern="beta")
    assert r["success"] is True
    assert r["count"] == 1
    assert r["matches"][0]["content"] == "beta"
    assert r["matches"][0]["line"] == 2


async def test_grep_regex(tmp_path):
    (tmp_path / "g.txt").write_text("cat\ncot\ncut\n", encoding="utf-8")
    r = await fsmod.fs(action="grep", path=str(tmp_path), pattern="c.t", regex=True)
    assert r["success"] is True
    assert r["count"] == 3


async def test_grep_ignore_case(tmp_path):
    (tmp_path / "g.txt").write_text("Hello\nhello\nHELLO\n", encoding="utf-8")
    r = await fsmod.fs(action="grep", path=str(tmp_path), pattern="hello", ignore_case=True)
    assert r["count"] == 3


async def test_grep_file_pattern(tmp_path):
    (tmp_path / "a.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("target\n", encoding="utf-8")
    r = await fsmod.fs(action="grep", path=str(tmp_path), pattern="target", file_pattern="*.py")
    assert r["count"] == 1
    assert r["matches"][0]["file"].endswith("a.py")


async def test_grep_context_lines(tmp_path):
    (tmp_path / "g.txt").write_text("l1\nl2\nHIT\nl4\nl5\n", encoding="utf-8")
    r = await fsmod.fs(action="grep", path=str(tmp_path), pattern="HIT", context_lines=1)
    m = r["matches"][0]
    assert m["before"] == ["2: l2"]
    assert m["after"] == ["4: l4"]


async def test_grep_invalid_regex(tmp_path):
    (tmp_path / "g.txt").write_text("x\n", encoding="utf-8")
    r = await fsmod.fs(action="grep", path=str(tmp_path), pattern="(unclosed", regex=True)
    assert r["success"] is False
    assert "regex" in r["error"].lower()


# --------------------- stat / diff / hash / touch / etc ---------------------

async def test_stat_file(tmp_path):
    p = tmp_path / "st.txt"
    p.write_text("abcde", encoding="utf-8")
    r = await fsmod.fs(action="stat", path=str(p))
    assert r["success"] is True and r["exists"] is True
    assert r["is_file"] is True and r["is_dir"] is False
    assert r["size"] == 5


async def test_stat_missing(tmp_path):
    r = await fsmod.fs(action="stat", path=str(tmp_path / "ghost"))
    assert r["success"] is True and r["exists"] is False


async def test_diff_strings(tmp_path):
    r = await fsmod.fs(action="diff", content_a="a\nb\n", content_b="a\nc\n")
    assert r["success"] is True
    assert r["changed"] is True
    assert r["added_lines"] == 1 and r["removed_lines"] == 1


async def test_diff_files(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("one\n", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("two\n", encoding="utf-8")
    r = await fsmod.fs(action="diff", path_a=str(a), path_b=str(b))
    assert r["success"] is True and r["changed"] is True


async def test_hash_known_value(tmp_path):
    p = tmp_path / "h.txt"
    p.write_bytes(b"abc")
    r = await fsmod.fs(action="hash", path=str(p), algorithms=["sha256", "md5"])
    assert r["success"] is True
    assert r["hashes"]["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert r["hashes"]["md5"] == hashlib.md5(b"abc").hexdigest()


async def test_hash_unknown_algo(tmp_path):
    p = tmp_path / "h.txt"
    p.write_bytes(b"abc")
    r = await fsmod.fs(action="hash", path=str(p), algorithms=["crc32"])
    assert r["success"] is False


async def test_touch_creates_file_and_parents(tmp_path):
    p = tmp_path / "sub" / "t.txt"
    r = await fsmod.fs(action="touch", path=str(p))
    assert r["success"] is True and p.exists()
    assert r["created"] is True


async def test_head_and_tail(tmp_path):
    p = tmp_path / "lines.txt"
    p.write_text("".join(f"l{i}\n" for i in range(1, 11)), encoding="utf-8")
    r = await fsmod.fs(action="head", path=str(p), lines=3)
    assert r["content"] == "l1\nl2\nl3\n"
    r = await fsmod.fs(action="tail", path=str(p), lines=2)
    assert r["content"] == "l9\nl10\n"


async def test_duplicates(tmp_path):
    (tmp_path / "x1.bin").write_bytes(b"identical-content")
    (tmp_path / "x2.bin").write_bytes(b"identical-content")
    (tmp_path / "y.bin").write_bytes(b"unique")
    r = await fsmod.fs(action="duplicates", path=str(tmp_path))
    assert r["success"] is True
    assert r["duplicate_groups"] == 1
    dup_files = {Path(f).name for g in r["duplicates"] for f in g["files"]}
    assert dup_files == {"x1.bin", "x2.bin"}


# --------------------------------- autofix ----------------------------------

async def test_autofix_strips_code_fence(tmp_path):
    p = tmp_path / "fenced.py"
    r = await fsmod.fs(action="write", path=str(p), content="```python\nx = 1\n```\n")
    assert r["success"] is True
    assert p.read_text(encoding="utf-8") == "x = 1\n"


async def test_autofix_normalizes_smart_quotes(tmp_path):
    p = tmp_path / "sq.txt"
    content = "He said “hello” and it’s fine\n"
    await fsmod.fs(action="write", path=str(p), content=content)
    assert p.read_text(encoding="utf-8") == 'He said "hello" and it\'s fine\n'


@pytest.mark.regression
async def test_autofix_preserves_inline_backticks(tmp_path):
    # REGRESSION: repair-only-if-broken. Valid python NOT starting with a fence
    # must survive verbatim, including a ``` sequence inside a string literal.
    p = tmp_path / "keep.py"
    content = 's = "```"\nprint(s)\n'
    r = await fsmod.fs(action="write", path=str(p), content=content)
    assert r["success"] is True
    assert p.read_text(encoding="utf-8") == content


async def test_write_rejects_broken_python(tmp_path):
    p = tmp_path / "broken.py"
    r = await fsmod.fs(action="write", path=str(p), content="def f(:\n    pass\n")
    assert r["success"] is False
    assert not p.exists()


# ------------------------------ misc dispatch -------------------------------

async def test_unknown_action():
    r = await fsmod.fs(action="frobnicate")
    assert r["success"] is False
    assert "Unknown action" in r["error"]


# --------------------------------- audit ------------------------------------

async def test_audit_snapshots_before_state(tmp_path, audit_on):
    # Forensic audit records previous contents before a destructive write, so any
    # overwrite is recoverable.
    fs_audit = audit_on["fs_audit"]
    p = tmp_path / "doc.txt"
    await fsmod.fs(action="write", path=str(p), content="ORIGINAL\n")
    await fsmod.fs(action="write", path=str(p), content="REPLACED\n")
    snaps = list(fs_audit.rglob("*write*"))
    assert snaps, "no write snapshots recorded"
    blob = "\n".join(s.read_text(encoding="utf-8", errors="replace") for s in snaps)
    assert "ORIGINAL" in blob
    assert p.read_text(encoding="utf-8") == "REPLACED\n"


# ---------------------- 2026-07-16 audit pass regressions -------------------

@pytest.mark.regression
async def test_write_omitted_content_is_rejected(tmp_path):
    # REGRESSION: `fs(action="write")` with content omitted once truncated the
    # target to empty - the `content` default of "" defeated the `is None` guard
    # meant to catch a forgotten payload. Omitting content must error and leave
    # the file byte-for-byte intact; an explicit "" is still a valid empty write.
    # (audit 2026-07-16)
    p = tmp_path / "keep.txt"
    p.write_text("IMPORTANT\n", encoding="utf-8")
    r = await fsmod.fs(action="write", path=str(p))  # content omitted entirely
    assert r["success"] is False
    assert "content required" in r["error"].lower()
    assert p.read_text(encoding="utf-8") == "IMPORTANT\n"  # NOT truncated
    # an explicit empty string remains a legitimate "make this file empty" write
    r = await fsmod.fs(action="write", path=str(p), content="")
    assert r["success"] is True
    assert p.read_text(encoding="utf-8") == ""


@pytest.mark.regression
async def test_edit_rollback_report_is_honest(tmp_path):
    # REGRESSION: a partial-fail edit batch already rolled back atomically, but
    # the response claimed changes_made:true with a full applied-looking diff and
    # match_results marking edits "applied" - all lies, since nothing hit disk.
    # The report must match disk truth. (commit 84efe0e)
    p = tmp_path / "r.py"
    original = "a = 1\nb = 2\n"
    p.write_text(original, encoding="utf-8")
    r = await fsmod.fs(action="edit", path=str(p), edits=[
        {"old_text": "a = 1", "new_text": "a = 10"},      # would match
        {"old_text": "NOT_PRESENT", "new_text": "q"},      # fails -> whole batch rolls back
    ])
    assert r["success"] is False
    assert r["written"] is False
    assert r["changes_made"] is False
    assert r["diff"] == ""
    assert "warning" in r and "ROLLED BACK" in r["warning"]
    statuses = {m["status"] for m in r["match_results"]}
    assert "applied" not in statuses          # the matched edit was relabeled
    assert "rolled_back" in statuses
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.regression
async def test_list_metadata_survives_bad_entry(tmp_path, monkeypatch):
    # REGRESSION: list(include_metadata=True) once failed the ENTIRE listing if a
    # single entry's stat() raised (broken symlink / Windows reparse point /
    # OneDrive placeholder). A bad entry must be reported inline, not be fatal.
    # (audit 2026-07-16)
    (tmp_path / "good.txt").write_text("x", encoding="utf-8")
    (tmp_path / "broken.txt").write_text("y", encoding="utf-8")

    real_stat = Path.stat
    def flaky_stat(self, *a, **k):
        if self.name == "broken.txt":
            raise OSError("simulated unreadable entry")
        return real_stat(self, *a, **k)
    monkeypatch.setattr(fsmod.Path, "stat", flaky_stat)

    r = await fsmod.fs(action="list", path=str(tmp_path), include_metadata=True)
    assert r["success"] is True
    by_name = {e["name"]: e for e in r["items"]}
    assert by_name["good.txt"]["type"] == "file"
    assert "error" in by_name["broken.txt"]     # bad entry reported, not fatal


async def test_duplicates_sha512(tmp_path):
    # sha512 must be an accepted algorithm (parity with hash_file's set; it was
    # missing from find_duplicates' map). (audit 2026-07-16)
    (tmp_path / "a.bin").write_bytes(b"identical")
    (tmp_path / "b.bin").write_bytes(b"identical")
    r = await fsmod.fs(action="duplicates", path=str(tmp_path), algorithm="sha512")
    assert r["success"] is True
    assert r["duplicate_groups"] == 1


def test_all_exports_every_public_action():
    # __all__ once omitted 6 of the 20 public helpers (diff/hash/touch/head/tail/
    # find_duplicates), so `from tools.filesystem import *` silently dropped them.
    # (audit 2026-07-16)
    for name in ("diff_content", "hash_file", "touch_file",
                 "head_file", "tail_file", "find_duplicates"):
        assert name in fsmod.__all__


@pytest.mark.regression
async def test_rejected_write_leaves_no_phantom_audit(tmp_path, audit_on):
    # REGRESSION: write_file logged a "write happened" before-state snapshot even
    # when the write was then rejected (syntax error, path conflict, or a
    # replace_lines limit<=0) and nothing touched disk - a phantom forensic entry
    # for a write that never occurred. The log now fires only after the guards.
    # (audit 2026-07-16)
    fs_audit = audit_on["fs_audit"]
    def write_snaps():
        return list(fs_audit.rglob("*write*"))

    good = tmp_path / "ok.txt"
    await fsmod.fs(action="write", path=str(good), content="v1\n")
    await fsmod.fs(action="write", path=str(good), content="v2\n")
    assert write_snaps(), "a real overwrite must record a before-state snapshot"
    base = len(write_snaps())

    # syntax-error write on a .py file: rejected before disk, no new snapshot
    r = await fsmod.fs(action="write", path=str(tmp_path / "bad.py"),
                       content="def broken(:\n    pass\n")
    assert r["success"] is False
    assert len(write_snaps()) == base

    # replace_lines with limit<=0: rejected before disk, no new snapshot
    r = await fsmod.fs(action="write", path=str(good), content="X\n", mode="replace_lines")
    assert r["success"] is False
    assert len(write_snaps()) == base
    assert good.read_text(encoding="utf-8") == "v2\n"  # untouched by the rejects


@pytest.mark.regression
async def test_audit_prune_caps_single_process_growth(tmp_path, audit_on, monkeypatch):
    # REGRESSION: the audit size cap once pruned only once per process (at the
    # first write), so a single long-lived session's OWN growth was never
    # re-checked - it grew unbounded. Pruning now runs on every write, at file
    # granularity, and must never delete the current session dir out from under
    # itself (a bug briefly introduced and caught during this fix). (audit 2026-07-16)
    fs_audit = audit_on["fs_audit"]
    monkeypatch.setattr(fsmod, "AUDIT_MAX_SIZE_MB", 1)   # 1 MB cap
    monkeypatch.setattr(fsmod, "AUDIT_SESSION_DIR", None)

    payload = "A" * 50_000
    target = tmp_path / "churn.txt"

    def folder_bytes():
        return sum(f.stat().st_size for f in fs_audit.rglob("*") if f.is_file())

    # 40 * ~50KB before-snapshots = ~2MB if never pruned; the cap must hold it down
    for i in range(40):
        await fsmod.fs(action="write", path=str(target), content=payload + str(i))

    cap = 1 * 1024 * 1024
    assert folder_bytes() <= cap * 1.5, "audit folder grew past the cap (prune not running per-write)"
    # the current session survived and still holds records => audit still working
    sess = fsmod.AUDIT_SESSION_DIR
    assert sess is not None and sess.exists()
    assert any(f.is_file() for f in sess.iterdir())


@pytest.mark.regression
async def test_copy_overwrite_snapshots_destination(tmp_path, audit_on):
    # REGRESSION: copy silently overwrote an existing destination file with no
    # audit trail - the one thing write_file's overwrite mode already guarded
    # against. copy must snapshot the clobbered content first. (audit 2026-07-16)
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new\n", encoding="utf-8")
    dst.write_text("OLD-TO-RECOVER\n", encoding="utf-8")
    r = await fsmod.fs(action="copy", path=str(src), destination=str(dst))
    assert r["success"] is True
    audited = list(audit_on["fs_audit"].rglob("*copy-overwrite*"))
    assert audited, "no audit snapshot for a copy that overwrote a destination file"
    assert "OLD-TO-RECOVER" in audited[0].read_text(encoding="utf-8")


@pytest.mark.regression
async def test_move_overwrite_snapshots_destination(tmp_path, audit_on):
    # REGRESSION: move silently overwrote an existing destination file (via
    # os.rename on POSIX or copy+unlink on Windows) with no audit trail.
    # (audit 2026-07-16)
    src = tmp_path / "src2.txt"
    dst = tmp_path / "dst2.txt"
    src.write_text("moved-in\n", encoding="utf-8")
    dst.write_text("DESTROYED-BY-MOVE\n", encoding="utf-8")
    r = await fsmod.fs(action="move", path=str(src), destination=str(dst))
    assert r["success"] is True
    audited = list(audit_on["fs_audit"].rglob("*move-overwrite*"))
    assert audited, "no audit snapshot for a move that overwrote a destination file"
    assert "DESTROYED-BY-MOVE" in audited[0].read_text(encoding="utf-8")


@pytest.mark.regression
async def test_append_and_insert_are_audited(tmp_path, audit_on):
    # REGRESSION: only overwrite/replace_lines were snapshotted; append/insert
    # changed a file's contents with zero audit trail. (audit 2026-07-16)
    p = tmp_path / "log.txt"
    p.write_text("base\n", encoding="utf-8")
    await fsmod.fs(action="write", path=str(p), content="more\n", mode="append")
    await fsmod.fs(action="write", path=str(p), content="top\n", mode="insert", offset=1)
    # audit filenames don't encode the write mode (always "..._write_<name>"),
    # so assert on count: before this fix, append/insert produced zero entries.
    audits = list(audit_on["fs_audit"].rglob("*_write_log.txt"))
    assert len(audits) == 2, audits
    assert "base" in audits[0].read_text(encoding="utf-8")


@pytest.mark.regression
async def test_head_tail_stream_large_file(tmp_path):
    # REGRESSION: head/tail called f.readlines() on the whole file regardless
    # of size, defeating the entire point of a head/tail tool for a file too
    # big to read in full. Now streamed: head stops after N lines, tail keeps
    # only a bounded deque. (audit 2026-07-16)
    p = tmp_path / "big.txt"
    with open(p, "w", encoding="utf-8") as f:
        for i in range(1, 50_001):
            f.write(f"line{i}\n")

    h = await fsmod.fs(action="head", path=str(p), lines=3)
    assert h["content"] == "line1\nline2\nline3\n"
    assert h["total_lines"] == 50_000
    assert h["has_more"] is True

    t = await fsmod.fs(action="tail", path=str(p), lines=3)
    assert t["content"] == "line49998\nline49999\nline50000\n"
    assert t["total_lines"] == 50_000
    assert t["start_line"] == 49998

    t0 = await fsmod.fs(action="tail", path=str(p), lines=0)
    assert t0["content"] == "" and t0["lines_returned"] == 0


@pytest.mark.regression
async def test_chunked_read_streams_not_whole_file(tmp_path):
    # REGRESSION: read_file's own comment claimed chunked (offset/limit) reads
    # "allow larger files since we're not loading all at once", but the
    # implementation called f.readlines() on the entire file regardless of
    # limit. Now actually streamed. (audit 2026-07-16)
    p = tmp_path / "chunked.txt"
    with open(p, "w", encoding="utf-8") as f:
        for i in range(1, 20_001):
            f.write(f"row{i}\n")

    r = await fsmod.fs(action="read", path=str(p), offset=10_000, limit=3)
    assert r["content"] == "row10000\nrow10001\nrow10002\n"
    assert r["total_lines"] == 20_000
    assert r["offset"] == 10_000
    assert r["has_more"] is True
