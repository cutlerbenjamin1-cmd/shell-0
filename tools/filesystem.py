# tools/filesystem.py
# Claude Code version - FULL ACCESS (no sandbox restrictions)

import shutil
import asyncio
import os
import json
import tempfile
import difflib
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

# =====================================================================================
# AUDIT LOGGING SYSTEM
# =====================================================================================

# Audit is ON by default. Repo-relative path so it never writes outside the repo.
# Relocate with FS_AUDIT_ROOT; disable entirely with SHELL0_AUDIT_DISABLE=1.
_DEFAULT_FS_AUDIT_ROOT = Path(__file__).resolve().parent.parent / "data" / "fs_audit"
AUDIT_DISABLED = os.getenv("SHELL0_AUDIT_DISABLE", "").strip().lower() in ("1", "true", "yes", "on")
AUDIT_ROOT = Path(os.getenv("FS_AUDIT_ROOT", str(_DEFAULT_FS_AUDIT_ROOT)))
AUDIT_MAX_SIZE_MB = int(os.getenv("FS_AUDIT_MAX_MB", "50"))
AUDIT_SESSION_DIR: Optional[Path] = None  # Initialized on first audit write


def _get_audit_session_dir() -> Path:
    """Get or create the current session's audit directory."""
    global AUDIT_SESSION_DIR
    if AUDIT_SESSION_DIR is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        AUDIT_SESSION_DIR = AUDIT_ROOT / timestamp
        AUDIT_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _prune_old_sessions()
    return AUDIT_SESSION_DIR


def _get_audit_folder_size() -> int:
    """Get total size of audit folder in bytes."""
    total = 0
    if AUDIT_ROOT.exists():
        for f in AUDIT_ROOT.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def _prune_old_sessions() -> None:
    """Delete oldest session folders until under size limit (FIFO)."""
    max_bytes = AUDIT_MAX_SIZE_MB * 1024 * 1024
    
    while _get_audit_folder_size() > max_bytes:
        # Get all session dirs sorted by name (oldest first due to timestamp format)
        sessions = sorted([d for d in AUDIT_ROOT.iterdir() if d.is_dir()])
        if len(sessions) <= 1:
            break  # Keep at least current session
        oldest = sessions[0]
        try:
            shutil.rmtree(oldest)
        except OSError:
            break


def _log_access(action: str, path: str, details: str = "") -> None:
    """Log a read/search access to access.log."""
    if AUDIT_DISABLED:
        return
    try:
        # Skip logging access to the audit directory itself (prevents noise)
        if str(AUDIT_ROOT) in path:
            return
        session_dir = _get_audit_session_dir()
        log_file = session_dir / "access.log"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"{timestamp} {action.upper()} {path}"
        if details:
            entry += f" [{details}]"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass  # Don't fail main operation due to logging


def _log_destructive_op(action: str, path: str, before_content: Optional[str] = None) -> None:
    """Log a destructive operation with before-state content."""
    if AUDIT_DISABLED:
        return
    try:
        session_dir = _get_audit_session_dir()
        timestamp = datetime.now().strftime("%H-%M-%S_%f")
        
        # Sanitize filename from path
        safe_name = Path(path).name.replace("/", "_").replace("\\", "_")
        if not safe_name:
            safe_name = "unknown"
        
        audit_file = session_dir / f"{timestamp}_{action}_{safe_name}"
        
        # Write before-state content
        content_to_write = before_content if before_content is not None else "[No previous content - new file]"
        with open(audit_file, "w", encoding="utf-8") as f:
            f.write(f"# AUDIT: {action.upper()} operation on {path}\n")
            f.write(f"# Timestamp: {datetime.now().isoformat()}\n")
            f.write(f"# Original path: {path}\n")
            f.write(f"# " + "="*70 + "\n\n")
            f.write(content_to_write)
    except Exception:
        pass  # Don't fail main operation due to logging


def _read_file_for_audit(path: Path) -> Optional[str]:
    """Read file content for audit logging. Returns None if unreadable."""
    try:
        if path.exists() and path.is_file():
            # Limit audit file size to 1MB
            if path.stat().st_size > 1024 * 1024:
                return f"[File too large for audit: {path.stat().st_size:,} bytes]"
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[Could not read for audit: {e}]"
    return None

# =====================================================================================
# CONFIGURATION - Claude Code version has FULL filesystem access
# =====================================================================================

MAX_READ_BYTES = int(os.getenv("FS_MAX_READ_BYTES", str(50 * 1024 * 1024)))  # 50MB default
MAX_TREE_DEPTH = int(os.getenv("FS_MAX_TREE_DEPTH", "15"))  # Deep traversal for projects
MAX_RECURSIVE_DELETE_DEPTH = int(os.getenv("FS_MAX_DELETE_DEPTH", "10"))

# No WORKING_ROOT restriction - full filesystem access
SANDBOXED = False


# =====================================================================================
# AUTO-FIXER FOR CODE FILES (ported from python_runner.py)
# =====================================================================================

import ast
import re

# File extensions that get Python-specific autofixing
CODE_EXTENSIONS = {'.py', '.pyw'}
# Extensions that get general text normalization
TEXT_EXTENSIONS = {'.py', '.pyw', '.js', '.ts', '.jsx', '.tsx', '.json', '.yaml', '.yml',
                   '.md', '.txt', '.html', '.css', '.scss', '.xml', '.toml', '.ini', '.cfg',
                   '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd'}


def _normalize_escaped_newlines(content: str) -> str:
    """Convert literal \\n sequences to actual newlines (LM Studio fix)."""
    if "\\n" in content:
        content = content.replace("\\r\\n", "\n").replace("\\n", "\n")
    return content


def _normalize_unicode_quotes(content: str) -> str:
    """Replace smart/curly quotes with straight ASCII quotes."""
    content = content.replace("\u201c", '"').replace("\u201d", '"')
    content = content.replace("\u2018", "'").replace("\u2019", "'")
    content = content.replace("\u2013", "-").replace("\u2014", "-")
    content = content.replace("\u2026", "...")
    return content


# Recognized fence language tags. Only these get stripped as a language line -
# anything else after ``` is treated as code (a bare `@dataclass` or `pass` on
# the first line must survive).
_FENCE_LANGS = {
    "python", "py", "python3", "javascript", "js", "typescript", "ts",
    "json", "yaml", "yml", "bash", "sh", "shell", "zsh", "powershell",
    "ps1", "bat", "cmd", "html", "css", "xml", "toml", "ini", "text",
    "txt", "plaintext", "markdown", "md", "jsx", "tsx", "sql", "c", "cpp",
    "csharp", "java", "rust", "go", "diff", "console",
}


def _strip_code_fences(content: str) -> str:
    """Strip markdown code fences from content, preserving internal indentation."""
    # Only strip if content actually has code fences
    test = content.strip()
    if not (test.startswith("```") or (test.startswith("`") and test.endswith("`"))):
        return content  # No fences, return as-is preserving all whitespace
    
    # Has fences - process them
    if test.startswith("```"):
        inner = test[3:].lstrip()  # Remove ``` and any space before language
        if "\n" in inner:
            first_line, rest = inner.split("\n", 1)
            lang = first_line.strip().lower()
            if lang in _FENCE_LANGS:
                inner = rest
        if inner.rstrip().endswith("```"):
            # Find the closing fence and remove it
            inner = inner.rstrip()[:-3]
        return inner  # Preserve internal indentation
    
    # Single backtick case (inline code)
    if test.startswith("`") and test.endswith("`") and test.count("`") == 2:
        return test[1:-1]  # Just remove the backticks
    
    return content


def _fix_json_booleans(content: str) -> str:
    """Convert JSON-style true/false/null to Python True/False/None."""
    if not any(tok in content for tok in ("true", "false", "null")):
        return content
    try:
        string_spans = [(m.start(), m.end()) for m in re.finditer(r'(["\'])(?:(?=(\\?))\2.)*?\1', content)]
        def in_string(pos):
            return any(start <= pos < end for start, end in string_spans)
        def replacer(match):
            if in_string(match.start()):
                return match.group(0)
            word = match.group(0)
            return {"true": "True", "false": "False", "null": "None"}.get(word, word)
        return re.sub(r'\b(true|false|null)\b', replacer, content)
    except Exception:
        return content


def _validate_python_syntax(content: str) -> tuple:
    """Validate Python syntax. Returns (is_valid, error_message)."""
    try:
        ast.parse(content, mode="exec")
        return True, None
    except SyntaxError as e:
        return False, f"Python syntax error at line {e.lineno}: {e.msg}"


def _autofix_content(content: str, path: str, validate: bool = True) -> tuple:
    """
    Apply autofixes to content based on file type.

    Repair-only-if-broken: for parseable formats (.py, .json) content that
    already parses is never modified, and a repair is only kept if it makes
    the content parse. Generic text gets the escaped-newline fix only when
    the content is a single-line blob (the LM Studio failure mode) - real
    multi-line files containing literal \\n (shell printf, regex, JSON-in-md)
    must not be touched.

    validate=False (chunked write modes): fragments aren't standalone-valid,
    so skip syntax rejection and parse-gated repairs; caller validates the
    merged result instead.

    Returns (fixed_content, warnings_list, validation_error).
    """
    warnings = []
    ext = Path(path).suffix.lower()

    if ext == ".json":
        def _parses(text):
            try:
                json.loads(text)
                return True
            except ValueError:
                return False

        if validate and not _parses(content):
            for candidate, label in (
                (_normalize_unicode_quotes(content), "Normalized Unicode quotes"),
                (_normalize_escaped_newlines(content), "Fixed escaped newlines"),
                (_normalize_escaped_newlines(_normalize_unicode_quotes(content)),
                 "Normalized Unicode quotes + fixed escaped newlines"),
            ):
                if candidate != content and _parses(candidate):
                    content = candidate
                    warnings.append(label)
                    break
        return content, warnings, None

    if ext in CODE_EXTENSIONS:
        original = content
        content = _strip_code_fences(content)
        if content != original:
            warnings.append("Stripped code fences")

        # NOTE: Removed _fix_json_booleans - it breaks JS code in Python f-strings
        # (e.g., visualize_kg.py has JS true/false that got converted to True/False)

        is_valid, error = _validate_python_syntax(content)
        if not is_valid:
            # Parse-gated repairs: curly quotes as delimiters, and \n-blob
            # newlines (only when there are no real newlines to begin with).
            candidates = [(_normalize_unicode_quotes(content), "Normalized Unicode quotes")]
            if "\n" not in content:
                candidates.append((_normalize_escaped_newlines(content), "Fixed escaped newlines"))
                candidates.append((_normalize_escaped_newlines(_normalize_unicode_quotes(content)),
                                   "Normalized Unicode quotes + fixed escaped newlines"))
            for candidate, label in candidates:
                if candidate != content and _validate_python_syntax(candidate)[0]:
                    content = candidate
                    warnings.append(label)
                    is_valid, error = True, None
                    break

        if validate and not is_valid:
            return content, warnings, error
        return content, warnings, None

    # Generic text files (and extensionless)
    if ext in TEXT_EXTENSIONS or not ext:
        original = content
        if "\n" not in content:
            content = _normalize_escaped_newlines(content)
            if content != original:
                warnings.append("Fixed escaped newlines")
                original = content

        content = _normalize_unicode_quotes(content)
        if content != original:
            warnings.append("Normalized Unicode quotes")

    return content, warnings, None


# =====================================================================================
# PATH RESOLUTION - Full access, accepts absolute or relative paths
# =====================================================================================

def _resolve_path(path: str) -> Path:
    r"""
    Resolve a path to absolute. Accepts:
    - Absolute paths (C:\..., /home/..., etc.)
    - Relative paths (resolved from cwd)

    No sandbox restrictions in Claude Code version.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid path parameter")

    p = Path(path.strip())

    # If absolute, use directly; otherwise resolve from cwd
    if p.is_absolute():
        return p.resolve()
    else:
        return Path.cwd().joinpath(p).resolve()


# =====================================================================================
# FILE OPERATIONS
# =====================================================================================

async def read_file(path: str, offset: int = 1, limit: int = 0) -> Dict[str, Any]:
    """
    Read a file with optional line-based chunking.

    Args:
        path: File path (absolute or relative)
        offset: Starting line number (1-based, default 1 = first line)
                Matches Claude's native Read tool line numbering.
        limit: Max lines to return (0 = all remaining lines)

    Returns:
        success, path, content, size, total_lines, offset (1-based), lines_returned
        If chunked: includes has_more flag

    Note: Line numbers are 1-based to match native Read tool output.
    """
    try:
        full = _resolve_path(path)
        
        # Audit logging - log read access with line details
        line_details = ""
        if offset > 1 or limit > 0:
            line_details = f"lines {offset}-{offset + limit - 1 if limit else 'end'}"
        _log_access("read", str(full), line_details)

        if not full.is_file():
            return {"success": False, "error": f"File not found: {full}"}

        size = full.stat().st_size

        # For chunked reads, allow larger files since we're not loading all at once
        if limit == 0 and size > MAX_READ_BYTES:
            return {
                "success": False,
                "error": f"File too large ({size:,} bytes). Max: {MAX_READ_BYTES:,} bytes. Use offset/limit for chunked reading."
            }

        def _read():
            # Try UTF-8 first, fall back to latin-1 for binary-ish files
            try:
                with open(full, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except UnicodeDecodeError:
                try:
                    with open(full, "r", encoding="latin-1") as f:
                        lines = f.readlines()
                except Exception:
                    return None, f"[Binary file: {size:,} bytes - cannot display as text]"
            return lines, None

        lines, error = await asyncio.to_thread(_read)

        if error:
            return {"success": True, "path": str(full), "content": error, "size": size, "binary": True}

        total_lines = len(lines)

        # Convert 1-based offset to 0-based internal index
        # offset=0 or offset=1 both mean "start from first line" (backwards compat)
        internal_offset = max(0, offset - 1) if offset > 0 else 0

        # Apply offset and limit
        if internal_offset > 0 or limit > 0:
            start = min(internal_offset, total_lines)
            end = total_lines if limit == 0 else min(start + limit, total_lines)
            selected_lines = lines[start:end]
            content = "".join(selected_lines)
            has_more = end < total_lines

            return {
                "success": True,
                "path": str(full),
                "content": content,
                "size": size,
                "total_lines": total_lines,
                "offset": start + 1,  # Return 1-based offset
                "lines_returned": len(selected_lines),
                "has_more": has_more
            }
        else:
            # Full file read
            content = "".join(lines)
            return {
                "success": True,
                "path": str(full),
                "content": content,
                "size": size,
                "total_lines": total_lines
            }

    except Exception as e:
        return {"success": False, "error": str(e)}

async def write_file(
    path: str,
    content: str,
    mode: str = "overwrite",
    offset: int = 1,
    limit: int = 0
) -> Dict[str, Any]:
    """
    Write content to a file with chunking support.

    Args:
        path: File path
        content: Content to write
        mode: Write mode - "overwrite" (default), "append", "insert", "replace_lines"
        offset: Starting line for insert/replace_lines mode (1-based, line 1 = first line)
                Matches Claude's native Read tool line numbering.
        limit: Number of lines to replace (for replace_lines mode, must be > 0)

    Returns:
        Dict with success, path, size, and metadata about the write operation

    Note: Line numbers are 1-based to match native Read tool output.
    """
    try:
        full = _resolve_path(path)
        dirpath = full.parent
        
        # Audit logging - log before-state for destructive writes
        if mode in ("overwrite", "replace_lines"):
            before_content = _read_file_for_audit(full)
            _log_destructive_op("write", str(full), before_content)

        # Apply autofixes; standalone syntax rejection only makes sense for
        # full overwrites - append/insert/replace_lines fragments are validated
        # against the merged result below instead.
        fixed_content, autofix_warnings, syntax_error = _autofix_content(
            content, str(full), validate=(mode == "overwrite")
        )
        
        # If Python syntax error, return error (don't write broken code)
        if syntax_error:
            return {
                "success": False,
                "error": syntax_error,
                "autofix_warnings": autofix_warnings,
                "hint": "Fix the syntax error and try again."
            }

        # Prevent conflict: dirpath exists as *file*
        if dirpath.exists() and not dirpath.is_dir():
            return {
                "success": False,
                "error": f"Path conflict: '{dirpath}' exists as a file, not a directory."
            }

        def _write_with_mode():
            dirpath.mkdir(parents=True, exist_ok=True)

            if mode == "overwrite":
                # Atomic overwrite: write to temp file, fsync, then replace
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(full.parent),
                    prefix=full.name,
                    suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(fixed_content)
                        f.flush()
                        os.fsync(f.fileno())
                    # On Windows, can't replace if dest exists - must delete first
                    os.replace(tmp_path, str(full))
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
                return {"mode": "overwrite"}

            elif mode == "append":
                # Append to end of file
                with open(full, "a", encoding="utf-8") as f:
                    f.write(fixed_content)
                result = {"mode": "append"}
                if full.suffix.lower() in CODE_EXTENSIONS:
                    try:
                        merged_ok, merged_err = _validate_python_syntax(
                            full.read_text(encoding="utf-8")
                        )
                        if not merged_ok:
                            result["merged_syntax_warning"] = f"Appended, but file now has a {merged_err}"
                    except Exception:
                        pass
                return result

            elif mode in ("insert", "replace_lines"):
                # Line-based operations
                existing_lines = []
                if full.exists():
                    with open(full, "r", encoding="utf-8") as f:
                        existing_lines = f.readlines()

                # Convert 1-based offset to 0-based internal index
                internal_offset = max(0, offset - 1) if offset > 0 else 0

                # Ensure content ends with newline for line-based ops
                content_lines = fixed_content.splitlines(keepends=True)
                if content_lines and not content_lines[-1].endswith(chr(10)):
                    content_lines[-1] += chr(10)

                if mode == "insert":
                    # Insert at offset, pushing existing lines down
                    new_lines = existing_lines[:internal_offset] + content_lines + existing_lines[internal_offset:]
                else:  # replace_lines
                    # Replace 'limit' lines starting at offset - limit must be > 0
                    if limit <= 0:
                        raise ValueError("replace_lines mode requires limit > 0 (number of lines to replace). Use 'insert' mode to add without replacing.")
                    end_idx = internal_offset + limit
                    new_lines = existing_lines[:internal_offset] + content_lines + existing_lines[end_idx:]

                with open(full, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)

                result = {
                    "mode": mode,
                    "offset": offset,  # Return user's 1-based offset
                    "lines_affected": limit if mode == "replace_lines" else 0,
                    "lines_written": len(content_lines),
                    "total_lines": len(new_lines)
                }
                if full.suffix.lower() in CODE_EXTENSIONS:
                    merged_ok, merged_err = _validate_python_syntax("".join(new_lines))
                    if not merged_ok:
                        result["merged_syntax_warning"] = f"File written, but merged result has a {merged_err}"
                return result

            else:
                raise ValueError(f"Unknown mode: {mode}. Valid: overwrite, append, insert, replace_lines")


        result = await asyncio.to_thread(_write_with_mode)
        response = {
            "success": True,
            "path": str(full),
            "size": len(fixed_content.encode("utf-8")),
            **result
        }
        if autofix_warnings:
            response["autofix_applied"] = autofix_warnings
        return response
    except Exception as e:
        return {"success": False, "error": str(e)}


async def delete_file(path: str) -> Dict[str, Any]:
    try:
        full = _resolve_path(path)

        if full.is_file():
            # Audit logging - save file content before delete
            before_content = _read_file_for_audit(full)
            _log_destructive_op("delete", str(full), before_content)
            
            await asyncio.to_thread(full.unlink)
            return {"success": True, "path": str(full)}

        return {"success": False, "error": "File not found"}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def copy_file(source: str, destination: str) -> Dict[str, Any]:
    try:
        full_src = _resolve_path(source)
        full_dst = _resolve_path(destination)

        if not full_src.is_file():
            return {"success": False, "error": "Source file not found"}

        dst_dir = full_dst.parent

        if dst_dir.exists() and not dst_dir.is_dir():
            return {
                "success": False,
                "error": f"Path conflict: '{dst_dir}' exists as a file, not a directory."
            }

        def _mkdir_and_copy():
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(full_src), str(full_dst))

        await asyncio.to_thread(_mkdir_and_copy)
        return {"success": True, "source": source, "destination": destination}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def move_path(source: str, destination: str) -> Dict[str, Any]:
    try:
        full_src = _resolve_path(source)
        full_dst = _resolve_path(destination)

        dst_dir = full_dst.parent

        if dst_dir.exists() and not dst_dir.is_dir():
            return {
                "success": False,
                "error": f"Path conflict: '{dst_dir}' exists as a file, not a directory."
            }

        def _mkdir_and_move():
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(full_src), str(full_dst))

        await asyncio.to_thread(_mkdir_and_move)
        return {"success": True, "source": source, "destination": destination}

    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# DIRECTORY OPERATIONS
# =====================================================================================

async def create_directory(path: str) -> Dict[str, Any]:
    try:
        full = _resolve_path(path)

        if full.exists() and not full.is_dir():
            return {
                "success": False,
                "error": f"Path conflict: '{full}' exists as a file, not a directory."
            }

        await asyncio.to_thread(full.mkdir, parents=True, exist_ok=True)
        return {"success": True, "path": path}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def delete_directory(path: str, recursive: bool = False, confirm: bool = False) -> Dict[str, Any]:
    try:
        full = _resolve_path(path)

        if not full.is_dir():
            return {"success": False, "error": f"Directory not found: {full}"}

        # Safety: prevent deleting system roots
        # Check all Windows drive letters, Unix root, home dir, and UNC paths
        drive_roots = [Path(f"{c}:/") for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
        danger_paths = [Path("/")] + drive_roots + [Path.home().resolve()]
        full_resolved = full.resolve()
        
        # Check if path is a danger path
        is_dangerous = (
            full_resolved in danger_paths or
            full in danger_paths or
            len(full.parts) <= 2 or
            # UNC root check: \\server or \\server\share
            (str(full).startswith("\\\\") and len(full.parts) <= 3)
        )
        if is_dangerous:
            return {"success": False, "error": f"Refusing to delete system/root path: {full}"}

        if recursive:
            def _count_items() -> int:
                return sum(len(files) + len(dirs) for _, dirs, files in os.walk(full))

            item_count = await asyncio.to_thread(_count_items)
            if item_count > 50 and not confirm:
                return {
                    "success": False,
                    "warning": f"Directory contains {item_count:,} items. Set confirm=True to proceed.",
                    "path": str(full),
                    "item_count": item_count,
                }
            
            # Audit logging - log directory listing before recursive delete
            def _get_listing():
                listing = []
                for root, dirs, files in os.walk(full):
                    for f in files:
                        listing.append(str(Path(root) / f))
                    if len(listing) > 1000:  # Cap listing size
                        listing.append(f"... and more (total: {item_count} items)")
                        return "\n".join(listing)
                return "\n".join(listing)
            
            dir_listing = await asyncio.to_thread(_get_listing)
            _log_destructive_op("rmdir", str(full), f"Recursive delete of {item_count} items:\n\n{dir_listing}")

            await asyncio.to_thread(shutil.rmtree, str(full))
            return {"success": True, "path": str(full), "items_removed": item_count}
        else:
            await asyncio.to_thread(full.rmdir)
            return {"success": True, "path": str(full)}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_directory(path: str = ".", include_metadata: bool = False) -> Dict[str, Any]:
    try:
        full = _resolve_path(path)

        if not full.is_dir():
            return {"success": False, "error": f"Directory not found: {full}"}

        def _list():
            entries = []
            for p in full.iterdir():
                if include_metadata:
                    stat = p.stat()
                    entries.append({
                        "name": p.name,
                        "type": "dir" if p.is_dir() else "file",
                        "size": stat.st_size if p.is_file() else None,
                        "mtime": stat.st_mtime
                    })
                else:
                    entries.append(p.name)
            return entries

        items = await asyncio.to_thread(_list)
        return {"success": True, "path": path, "items": items}

    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# DIRECTORY TREE (RECURSIVE LIST)
# =====================================================================================

async def directory_tree(path: str = ".") -> Dict[str, Any]:
    """Return a recursive tree structure of directories and files."""
    try:
        root = _resolve_path(path)

        if not root.is_dir():
            return {"success": False, "error": f"Directory not found: {root}"}

        def _walk():
            tree = {}
            for dirpath, dirnames, filenames in os.walk(root):
                rel_path = Path(dirpath).relative_to(root)
                depth = len(rel_path.parts)
                if depth >= MAX_TREE_DEPTH:
                    dirnames[:] = []  # prune deeper traversal

                rel = str(rel_path)
                if rel == ".":
                    rel = ""
                tree[rel] = {
                    "directories": dirnames.copy(),
                    "files": filenames
                }
            return tree

        result = await asyncio.to_thread(_walk)
        return {"success": True, "root": str(root), "tree": result}

    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# SEARCH UTILITIES
# =====================================================================================

async def search_files(path: str, pattern: str) -> Dict[str, Any]:
    """Filename search - supports substring match or glob patterns (*, ?)."""
    try:
        full = _resolve_path(path)

        if not full.is_dir():
            return {"success": False, "error": f"Directory not found: {full}"}
        
        # Audit logging - log file search
        _log_access("search", str(full), f"pattern='{pattern}'")

        def _search():
            import fnmatch

            matched: List[str] = []
            # Determine if pattern is glob-style or substring
            is_glob = "*" in pattern or "?" in pattern

            for dirpath, dirnames, filenames in os.walk(full):
                rel_depth = len(Path(dirpath).relative_to(full).parts)
                if rel_depth >= MAX_TREE_DEPTH:
                    dirnames[:] = []  # stop descending further

                for filename in filenames:
                    if is_glob:
                        # Case-insensitive glob matching
                        if fnmatch.fnmatch(filename.lower(), pattern.lower()):
                            matched.append(str(Path(dirpath, filename)))
                    else:
                        if pattern.lower() in filename.lower():
                            matched.append(str(Path(dirpath, filename)))
            return matched

        matched = await asyncio.to_thread(_search)

        return {"success": True, "root": str(full), "pattern": pattern, "matches": matched, "count": len(matched)}

    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================


# =====================================================================================
# EDIT OPERATION (pattern-based text replacement)
# =====================================================================================

import difflib

async def edit_file(
    path: str,
    edits: List[Dict[str, str]],
    dry_run: bool = False,
    preserve_indentation: bool = True
) -> Dict[str, Any]:
    """
    Edit a file using pattern-based text replacement.

    Args:
        path: File path
        edits: List of {"old_text": str, "new_text": str} replacements
        dry_run: If True, return diff without writing
        preserve_indentation: Apply old_text indentation to new_text

    Returns:
        success, diff, match_results, edits_applied, edits_failed
    """
    try:
        full = _resolve_path(path)
        if not full.is_file():
            return {"success": False, "error": f"File not found: {full}"}

        with open(full, "r", encoding="utf-8") as f:
            original = f.read()
        
        # Audit logging - log before-state (only for actual edits, not dry_run)
        if not dry_run:
            _log_destructive_op("edit", str(full), original)

        current = original
        match_results = []
        edits_applied = 0
        edits_failed = 0

        for i, edit in enumerate(edits):
            old_text = edit.get("old_text", "")
            new_text = edit.get("new_text", "")

            if not old_text:
                match_results.append({"edit_index": i, "status": "failed", "reason": "empty old_text"})
                edits_failed += 1
                continue

            if old_text == new_text:
                match_results.append({"edit_index": i, "status": "skipped", "reason": "no change needed"})
                continue

            if old_text in current:
                # Apply indentation preservation
                final_new = new_text
                if preserve_indentation:
                    old_lines = old_text.split("\n")
                    new_lines = new_text.split("\n")
                    if old_lines and new_lines:
                        old_indent = ""
                        for c in old_lines[0]:
                            if c in " \t":
                                old_indent += c
                            else:
                                break
                        new_indent = ""
                        for c in new_lines[0]:
                            if c in " \t":
                                new_indent += c
                            else:
                                break
                        if old_indent and not new_indent:
                            final_new = "\n".join(old_indent + line if line.strip() else "" for line in new_lines)

                pos = current.find(old_text)
                line_num = current[:pos].count("\n") + 1
                current = current.replace(old_text, final_new, 1)
                match_results.append({"edit_index": i, "status": "applied", "line": line_num})
                edits_applied += 1
            elif len(new_text) >= 30 and new_text in current:
                # "Already applied" heuristic: only trust it for substantial
                # snippets. Short new_text (a docstring, a closing brace) is
                # almost always coincidentally present, masking a real
                # old_text mismatch as a skip.
                match_results.append({"edit_index": i, "status": "skipped", "reason": "already applied"})
            else:
                match_results.append({"edit_index": i, "status": "failed", "reason": "text not found"})
                edits_failed += 1

        changes_made = current != original
        diff = ""
        if changes_made:
            diff = "".join(difflib.unified_diff(
                original.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"a/{full.name}",
                tofile=f"b/{full.name}"
            ))

        if not dry_run and changes_made and edits_failed == 0:
            # Atomic write
            import tempfile
            fd, temp = tempfile.mkstemp(dir=str(full.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(current)
                os.replace(temp, str(full))
            except Exception:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise

        return {
            "success": edits_failed == 0,
            "path": str(full),
            "dry_run": dry_run,
            "changes_made": changes_made,
            "diff": diff,
            "match_results": match_results,
            "edits_applied": edits_applied,
            "edits_failed": edits_failed
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# GREP OPERATION (content search within files)
# =====================================================================================

async def grep_files(
    path: str,
    pattern: str,
    max_depth: int = 10,
    max_results: int = 500,
    file_pattern: str = "*",
    ignore_case: bool = False,
    regex: bool = False,
    context_lines: int = 0,
    max_line_length: int = 500
) -> Dict[str, Any]:
    """
    Search for text/regex within file contents.

    Args:
        path: Directory to search
        pattern: Text or regex pattern to search for
        max_depth: Max directory depth
        max_results: Max matches to return
        file_pattern: Glob pattern to filter files (e.g., "*.py") - case insensitive
        ignore_case: Case insensitive search
        regex: Treat pattern as regex (default: substring match)
        context_lines: Number of lines before/after match to include
        max_line_length: Max chars per line in output (default 500)
    """
    import fnmatch
    import re

    try:
        full = _resolve_path(path)
        is_single_file = full.is_file()
        
        if not full.exists():
            return {"success": False, "error": f"Path not found: {full}"}
        if not is_single_file and not full.is_dir():
            return {"success": False, "error": f"Path is not a file or directory: {full}"}
        
        # Audit logging - log grep search
        _log_access("grep", str(full), f"pattern='{pattern}' files='{file_pattern}' regex={regex} ignore_case={ignore_case}")
        
        # Compile regex or prepare matcher
        if regex:
            flags = re.IGNORECASE if ignore_case else 0
            try:
                compiled = re.compile(pattern, flags)
            except re.error as e:
                return {"success": False, "error": f"Invalid regex: {e}"}
            def matches(line):
                return compiled.search(line) is not None
        else:
            if ignore_case:
                pattern_lower = pattern.lower()
                def matches(line):
                    return pattern_lower in line.lower()
            else:
                def matches(line):
                    return pattern in line

        def _grep_single_file(filepath: Path):
            """Search a single file and return results."""
            results = []
            try:
                if filepath.stat().st_size > 2 * 1024 * 1024:  # 2MB
                    return results
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                
                for line_num, line in enumerate(lines, 1):
                    if len(results) >= max_results:
                        break
                    if matches(line):
                        result = {
                            "file": str(filepath),
                            "line": line_num,
                            "content": line.rstrip()[:max_line_length]
                        }
                        if context_lines > 0:
                            before = []
                            after = []
                            for i in range(max(0, line_num - 1 - context_lines), line_num - 1):
                                before.append(f"{i+1}: {lines[i].rstrip()[:max_line_length]}")
                            for i in range(line_num, min(len(lines), line_num + context_lines)):
                                after.append(f"{i+1}: {lines[i].rstrip()[:max_line_length]}")
                            if before:
                                result["before"] = before
                            if after:
                                result["after"] = after
                        results.append(result)
            except (OSError, UnicodeDecodeError):
                pass
            return results

        # Handle single file grep
        if is_single_file:
            results = await asyncio.to_thread(_grep_single_file, full)
            return {
                "success": True,
                "root": str(full.parent),
                "file": str(full),
                "pattern": pattern,
                "regex": regex,
                "ignore_case": ignore_case,
                "matches": results,
                "count": len(results),
                "files_matched": 1 if results else 0,
                "files_searched": 1,
                "truncated": len(results) >= max_results
            }

        def _grep():
            results = []
            files_searched = 0
            files_with_matches = set()

            for dirpath, dirnames, filenames in os.walk(full):
                rel_depth = len(Path(dirpath).relative_to(full).parts)
                if rel_depth >= max_depth:
                    dirnames[:] = []
                    continue

                for filename in filenames:
                    if len(results) >= max_results:
                        return results, files_searched, len(files_with_matches), True

                    # Case-insensitive file pattern matching
                    if not fnmatch.fnmatch(filename.lower(), file_pattern.lower()):
                        continue

                    filepath = Path(dirpath) / filename

                    # Skip large files
                    try:
                        if filepath.stat().st_size > 2 * 1024 * 1024:  # 2MB
                            continue
                    except OSError:
                        continue

                    # Try to read as text
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()
                        files_searched += 1
                        
                        for line_num, line in enumerate(lines, 1):
                            if matches(line):
                                files_with_matches.add(str(filepath))
                                
                                # Build result with context
                                result = {
                                    "file": str(filepath),
                                    "line": line_num,
                                    "content": line.rstrip()[:max_line_length]
                                }
                                
                                # Add context lines if requested
                                if context_lines > 0:
                                    before = []
                                    after = []
                                    for i in range(max(0, line_num - 1 - context_lines), line_num - 1):
                                        before.append(f"{i+1}: {lines[i].rstrip()[:max_line_length]}")
                                    for i in range(line_num, min(len(lines), line_num + context_lines)):
                                        after.append(f"{i+1}: {lines[i].rstrip()[:max_line_length]}")
                                    if before:
                                        result["before"] = before
                                    if after:
                                        result["after"] = after
                                
                                results.append(result)
                                if len(results) >= max_results:
                                    return results, files_searched, len(files_with_matches), True
                    except (OSError, UnicodeDecodeError):
                        continue

            return results, files_searched, len(files_with_matches), False

        results, files_searched, files_matched, truncated = await asyncio.to_thread(_grep)

        return {
            "success": True,
            "root": str(full),
            "pattern": pattern,
            "regex": regex,
            "ignore_case": ignore_case,
            "matches": results,
            "count": len(results),
            "files_matched": files_matched,
            "files_searched": files_searched,
            "truncated": truncated
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# STAT OPERATION (file metadata)
# =====================================================================================

async def stat_file(path: str) -> Dict[str, Any]:
    """Get detailed file/directory metadata."""
    try:
        full = _resolve_path(path)

        if not full.exists():
            return {"success": True, "path": str(full), "exists": False}

        st = full.stat()

        return {
            "success": True,
            "path": str(full),
            "exists": True,
            "is_file": full.is_file(),
            "is_dir": full.is_dir(),
            "is_symlink": full.is_symlink(),
            "size": st.st_size,
            "created": st.st_ctime,
            "modified": st.st_mtime,
            "accessed": st.st_atime,
            "permissions": oct(st.st_mode)[-3:]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# DIFF OPERATION (compare files or content)
# =====================================================================================

async def diff_content(
    path_a: str = "",
    path_b: str = "",
    content_a: str = "",
    content_b: str = "",
    context_lines: int = 3,
    output_format: str = "unified"
) -> Dict[str, Any]:
    """Compare two files or two strings and return a diff."""
    try:
        if path_a:
            full_a = _resolve_path(path_a)
            if not full_a.is_file():
                return {"success": False, "error": f"File not found: {path_a}"}
            text_a = full_a.read_text(encoding="utf-8", errors="replace")
            label_a = path_a
        else:
            text_a = content_a or ""
            label_a = "a"

        if path_b:
            full_b = _resolve_path(path_b)
            if not full_b.is_file():
                return {"success": False, "error": f"File not found: {path_b}"}
            text_b = full_b.read_text(encoding="utf-8", errors="replace")
            label_b = path_b
        else:
            text_b = content_b or ""
            label_b = "b"

        lines_a = text_a.splitlines(keepends=True)
        lines_b = text_b.splitlines(keepends=True)

        if output_format == "context":
            diff_lines = list(difflib.context_diff(lines_a, lines_b, fromfile=label_a, tofile=label_b, n=context_lines))
        elif output_format == "ndiff":
            diff_lines = list(difflib.ndiff(lines_a, lines_b))
        else:
            diff_lines = list(difflib.unified_diff(lines_a, lines_b, fromfile=label_a, tofile=label_b, n=context_lines))

        diff_text = "".join(diff_lines)
        added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

        return {"success": True, "diff_text": diff_text, "added_lines": added, "removed_lines": removed, "changed": added > 0 or removed > 0, "format": output_format}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# HASH OPERATION (compute file checksums)
# =====================================================================================

async def hash_file(path: str, algorithms: List[str] = None) -> Dict[str, Any]:
    """Compute hash(es) of a file."""
    if algorithms is None:
        algorithms = ["sha256"]
    try:
        full = _resolve_path(path)
        if not full.is_file():
            return {"success": False, "error": f"File not found: {path}"}

        size = full.stat().st_size
        algo_map = {"md5": hashlib.md5, "sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}
        hashers = {}
        for algo in algorithms:
            if algo.lower() not in algo_map:
                return {"success": False, "error": f"Unknown algorithm: {algo}"}
            hashers[algo.lower()] = algo_map[algo.lower()]()

        def _hash():
            with open(full, "rb") as f:
                while chunk := f.read(65536):
                    for h in hashers.values():
                        h.update(chunk)
            return {name: h.hexdigest() for name, h in hashers.items()}

        hashes = await asyncio.to_thread(_hash)
        return {"success": True, "path": str(full), "size": size, "hashes": hashes}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# TOUCH OPERATION (create empty file or update mtime)
# =====================================================================================

async def touch_file(path: str, create_parents: bool = True) -> Dict[str, Any]:
    """Create an empty file or update its modification time."""
    try:
        full = _resolve_path(path)
        existed = full.exists()
        if create_parents:
            full.parent.mkdir(parents=True, exist_ok=True)
        full.touch()
        return {"success": True, "path": str(full), "created": not existed, "mtime": full.stat().st_mtime}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# HEAD/TAIL OPERATIONS
# =====================================================================================

async def head_file(path: str, lines: int = 10) -> Dict[str, Any]:
    """Read the first N lines of a file."""
    try:
        full = _resolve_path(path)
        if not full.is_file():
            return {"success": False, "error": f"File not found: {path}"}

        def _read():
            try:
                with open(full, "r", encoding="utf-8") as f:
                    return f.readlines()
            except UnicodeDecodeError:
                with open(full, "r", encoding="latin-1") as f:
                    return f.readlines()

        all_lines = await asyncio.to_thread(_read)
        selected = all_lines[:lines]
        return {"success": True, "path": str(full), "content": "".join(selected), "lines_returned": len(selected), "total_lines": len(all_lines), "has_more": len(all_lines) > lines}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tail_file(path: str, lines: int = 10) -> Dict[str, Any]:
    """Read the last N lines of a file."""
    try:
        full = _resolve_path(path)
        if not full.is_file():
            return {"success": False, "error": f"File not found: {path}"}

        def _read():
            try:
                with open(full, "r", encoding="utf-8") as f:
                    return f.readlines()
            except UnicodeDecodeError:
                with open(full, "r", encoding="latin-1") as f:
                    return f.readlines()

        all_lines = await asyncio.to_thread(_read)
        selected = all_lines[-lines:] if lines < len(all_lines) else all_lines
        return {"success": True, "path": str(full), "content": "".join(selected), "lines_returned": len(selected), "total_lines": len(all_lines), "start_line": max(1, len(all_lines) - lines + 1)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# FIND DUPLICATES (by hash)
# =====================================================================================

async def find_duplicates(path: str = "", pattern: str = "*", algorithm: str = "sha256", min_size: int = 1) -> Dict[str, Any]:
    """Find duplicate files by content hash."""
    try:
        full = _resolve_path(path) if path else Path.cwd()
        if not full.is_dir():
            return {"success": False, "error": f"Directory not found: {path}"}

        algo_map = {"md5": hashlib.md5, "sha1": hashlib.sha1, "sha256": hashlib.sha256}
        if algorithm.lower() not in algo_map:
            return {"success": False, "error": f"Unknown algorithm: {algorithm}"}
        hash_func = algo_map[algorithm.lower()]

        def _scan():
            hash_to_files = {}
            total = 0
            for file_path in full.rglob(pattern):
                if not file_path.is_file():
                    continue
                try:
                    size = file_path.stat().st_size
                    if size < min_size:
                        continue
                    total += 1
                    h = hash_func()
                    with open(file_path, "rb") as f:
                        while chunk := f.read(65536):
                            h.update(chunk)
                    digest = h.hexdigest()
                    if digest not in hash_to_files:
                        hash_to_files[digest] = []
                    hash_to_files[digest].append({"path": str(file_path), "size": size})
                except (PermissionError, OSError):
                    continue
            return hash_to_files, total

        hash_to_files, total_files = await asyncio.to_thread(_scan)
        duplicates = []
        wasted = 0
        dup_count = 0
        for digest, files in hash_to_files.items():
            if len(files) > 1:
                duplicates.append({"hash": digest[:16] + "...", "count": len(files), "size": files[0]["size"], "files": [f["path"] for f in files]})
                wasted += (len(files) - 1) * files[0]["size"]
                dup_count += len(files) - 1

        return {"success": True, "path": str(full), "algorithm": algorithm, "total_files": total_files, "duplicate_groups": len(duplicates), "duplicate_files": dup_count, "wasted_bytes": wasted, "duplicates": duplicates[:50]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =====================================================================================
# CONSOLIDATED FS TOOL (for MCP token efficiency)
# =====================================================================================

async def fs(
    action: str,
    path: str = "",
    content: str = "",
    destination: str = "",
    pattern: str = "",
    recursive: bool = False,
    confirm: bool = False,
    include_metadata: bool = False,
    offset: int = 1,
    limit: int = 0,
    mode: str = "overwrite",
    **kwargs
) -> Dict[str, Any]:
    """
    Unified filesystem tool. Actions: read, write, edit, delete, copy, move, mkdir, rmdir, list, tree, search, grep, stat.

    NEW in v2:
    - edit: Pattern-based text replacement with diff preview
    - grep: Content search within files
    - stat: File metadata (size, times, permissions)

    Args:
        action: Operation to perform (read|write|delete|copy|move|mkdir|rmdir|list|tree|search)
        path: Target file/directory path (required for most actions)
        content: File content (for write action)
        destination: Destination path (for copy/move actions)
        pattern: Search pattern (for search action)
        recursive: Enable recursive delete (for rmdir action)
        confirm: Confirm large recursive delete (for rmdir action)
        include_metadata: Include file metadata in list results
        offset: Starting line (1-based, matches native Read tool) for chunked read/write
        limit: Max lines for chunked read (0 = all), or lines to replace in replace_lines mode
    """
    action = action.lower().strip()

    # Dispatch to appropriate function
    if action == "read":
        if not path:
            return {"success": False, "error": "path required for read action"}
        return await read_file(path, offset=offset, limit=limit)

    elif action == "write":
        if not path:
            return {"success": False, "error": "path required for write action"}
        if content is None:
            return {"success": False, "error": "content required for write action"}
        return await write_file(path, content, mode=mode, offset=offset, limit=limit)

    elif action == "delete":
        if not path:
            return {"success": False, "error": "path required for delete action"}
        return await delete_file(path)

    elif action == "copy":
        if not path or not destination:
            return {"success": False, "error": "path and destination required for copy action"}
        return await copy_file(path, destination)

    elif action == "move":
        if not path or not destination:
            return {"success": False, "error": "path and destination required for move action"}
        return await move_path(path, destination)

    elif action == "mkdir":
        if not path:
            return {"success": False, "error": "path required for mkdir action"}
        return await create_directory(path)

    elif action == "rmdir":
        if not path:
            return {"success": False, "error": "path required for rmdir action"}
        return await delete_directory(path, recursive=recursive, confirm=confirm)

    elif action == "list":
        return await list_directory(path or ".", include_metadata=include_metadata)

    elif action == "tree":
        return await directory_tree(path or ".")

    elif action == "search":
        if not pattern:
            return {"success": False, "error": "pattern required for search action"}
        return await search_files(path or ".", pattern)

    elif action == "edit":
        if not path:
            return {"success": False, "error": "path required for edit action"}
        edits = kwargs.get("edits", [])
        if not edits:
            return {"success": False, "error": "edits list required for edit action"}
        dry_run = kwargs.get("dry_run", False)
        preserve_indentation = kwargs.get("preserve_indentation", True)
        return await edit_file(path, edits, dry_run=dry_run, preserve_indentation=preserve_indentation)

    elif action == "grep":
        if not pattern:
            return {"success": False, "error": "pattern required for grep action"}
        max_depth = kwargs.get("max_depth", 10)
        max_results = kwargs.get("max_results", 500)
        file_pattern = kwargs.get("file_pattern", "*")
        ignore_case = kwargs.get("ignore_case", False)
        regex = kwargs.get("regex", False)
        context_lines = kwargs.get("context_lines", 0)
        max_line_length = kwargs.get("max_line_length", 500)
        return await grep_files(
            path or ".", pattern, 
            max_depth=max_depth, max_results=max_results, file_pattern=file_pattern,
            ignore_case=ignore_case, regex=regex, context_lines=context_lines, 
            max_line_length=max_line_length
        )

    elif action == "stat":
        if not path:
            return {"success": False, "error": "path required for stat action"}
        return await stat_file(path)

    elif action == "diff":
        path_a = kwargs.get("path_a", path)
        path_b = kwargs.get("path_b", destination)
        content_a = kwargs.get("content_a", "")
        content_b = kwargs.get("content_b", content)
        context_lines = kwargs.get("context_lines", 3)
        output_format = kwargs.get("output_format", "unified")
        return await diff_content(path_a=path_a, path_b=path_b, content_a=content_a, content_b=content_b, context_lines=context_lines, output_format=output_format)

    elif action == "hash":
        if not path:
            return {"success": False, "error": "path required for hash action"}
        algorithms = kwargs.get("algorithms", ["sha256"])
        return await hash_file(path, algorithms=algorithms)

    elif action == "touch":
        if not path:
            return {"success": False, "error": "path required for touch action"}
        create_parents = kwargs.get("create_parents", True)
        return await touch_file(path, create_parents=create_parents)

    elif action == "head":
        if not path:
            return {"success": False, "error": "path required for head action"}
        lines = kwargs.get("lines", limit if limit > 0 else 10)
        return await head_file(path, lines=lines)

    elif action == "tail":
        if not path:
            return {"success": False, "error": "path required for tail action"}
        lines = kwargs.get("lines", limit if limit > 0 else 10)
        return await tail_file(path, lines=lines)

    elif action == "duplicates":
        file_pattern = kwargs.get("file_pattern", pattern or "*")
        algorithm = kwargs.get("algorithm", "sha256")
        min_size = kwargs.get("min_size", 1)
        return await find_duplicates(path=path, pattern=file_pattern, algorithm=algorithm, min_size=min_size)

    else:
        return {
            "success": False,
            "error": f"Unknown action: {action}. Valid: read, write, edit, delete, copy, move, mkdir, rmdir, list, tree, search, grep, stat, diff, hash, touch, head, tail, duplicates"
        }


# Exported for server registration
__all__ = ["fs", "read_file", "write_file", "edit_file", "delete_file", "copy_file",
           "move_path", "create_directory", "delete_directory",
           "list_directory", "directory_tree", "search_files", "grep_files", "stat_file"]
