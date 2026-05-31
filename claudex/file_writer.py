"""Safe file writer with backup support and path confinement."""

import shutil
from pathlib import Path, PureWindowsPath

from .models import FileOutput


class UnsafePathError(Exception):
    """Raised when a generated path would escape or abuse the target directory."""


# Windows reserved device names — special regardless of extension or case
# (NUL.txt is still NUL). Rejected on the model-generated path surface.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _is_within(child: Path, base: Path) -> bool:
    """True if `child` is inside `base` (inclusive)."""
    try:  # Path.is_relative_to is py3.9+
        return child.is_relative_to(base)
    except AttributeError:  # portability fallback for <3.9
        try:
            child.relative_to(base)
            return True
        except ValueError:
            return False


def safe_resolve(target_dir: Path, rel: str) -> Path:
    """Resolve `rel` under `target_dir`, rejecting anything that escapes it.

    Rejects absolute paths, drive-qualified paths (C:\\...), UNC paths
    (\\\\host\\share), leading-slash roots, any `..` parent traversal, and any
    path that resolves outside `target_dir`. A model emitting such a path is a
    signal, not a typo — callers should fail loudly, never silently drop.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise UnsafePathError(f"empty or non-string path: {rel!r}")

    # Interpret with Windows semantics so drive letters / backslashes are caught
    # even when running on POSIX.
    pw = PureWindowsPath(rel)
    if pw.is_absolute() or pw.drive or pw.root:
        raise UnsafePathError(f"absolute/drive/UNC path rejected: {rel!r}")
    if rel.startswith(("\\\\", "//", "/", "\\")):
        raise UnsafePathError(f"rooted/UNC path rejected: {rel!r}")

    parts = pw.parts
    if ".." in parts:
        raise UnsafePathError(f"parent traversal rejected: {rel!r}")

    # Per-segment Windows hazards that don't necessarily escape target_dir but
    # behave unlike normal files: alternate data streams (colon) and reserved
    # device names (NUL, CON, COM1, ...).
    for part in parts:
        if ":" in part:
            raise UnsafePathError(f"alternate-data-stream/colon segment rejected: {rel!r}")
        base_name = part.split(".")[0].strip().upper()
        if base_name in _RESERVED_NAMES:
            raise UnsafePathError(f"reserved Windows device name rejected: {rel!r}")

    # Build from normalized parts (not the raw string) so backslash separators
    # resolve consistently on POSIX as well as Windows.
    base = target_dir.resolve()
    resolved = base.joinpath(*parts).resolve()
    if not _is_within(resolved, base):
        raise UnsafePathError(f"path escapes target dir: {rel!r}")
    return resolved


def write_files(files: list[FileOutput], target_dir: Path, backup: bool = True) -> list[str]:
    """Write generated files to disk with optional backups.

    All paths are validated up front (Wave 1.1 path confinement). If ANY path is
    unsafe, nothing is written and UnsafePathError is raised listing every
    offending path. NOTE: this guards the *path surface* only — it is not a
    write-time rollback. If every path validates but a write/copy/delete fails
    partway through the batch, earlier files may already be changed. (A future
    wave can add staging + rollback for true write-time atomicity.)

    Args:
        files: List of FileOutput objects to write
        target_dir: Base directory for file output
        backup: If True, create .claudex.bak for overwritten files

    Returns:
        List of summary strings describing what was written

    Raises:
        UnsafePathError: if any file path would escape target_dir.
    """
    # --- Pre-flight: validate every path before touching disk ---
    resolved_paths: list[Path] = []
    unsafe: list[str] = []
    for f in files:
        try:
            resolved_paths.append(safe_resolve(target_dir, f.path))
        except UnsafePathError as e:
            unsafe.append(str(e))
    if unsafe:
        raise UnsafePathError(
            "refusing to write — unsafe path(s) detected:\n  - "
            + "\n  - ".join(unsafe)
        )

    summaries = []

    for f, file_path in zip(files, resolved_paths):
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if f.action == "delete":
            if file_path.exists():
                if backup:
                    shutil.copy2(file_path, str(file_path) + ".claudex.bak")
                file_path.unlink()
                summaries.append(f"  - {f.path} (deleted, backup saved)")
            else:
                summaries.append(f"  - {f.path} (already absent, skipped)")

        elif f.action in ("create", "modify"):
            existed = file_path.exists()

            if existed and backup:
                shutil.copy2(file_path, str(file_path) + ".claudex.bak")

            file_path.write_text(f.content, encoding="utf-8")

            line_count = f.content.count("\n") + 1
            action_label = "modified" if existed else "created"
            backup_note = ", backup saved" if existed and backup else ""
            summaries.append(f"  + {f.path} ({action_label}, {line_count} lines{backup_note})")

    return summaries
