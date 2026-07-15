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
