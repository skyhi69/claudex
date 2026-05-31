"""Search/replace + full-file edit engine (Wave 2A).

Codex (running read-only, grounded in the real repo) emits edits as text in one
of two block formats; claudex parses and applies them — the model never writes
to disk itself. Apply is path-confined (reuses file_writer.safe_resolve) and
transactional at the validation layer: if ANY edit fails to validate, nothing is
written.

EDIT block (preferred — surgical, robust):

    === EDIT: relative/path.py ===
    <<<<<<< SEARCH
    <exact existing lines, verbatim>
    =======
    <replacement lines>
    >>>>>>> REPLACE
    === END EDIT ===

FILE block (new files / small files / full replacement):

    === FILE: relative/new.py ===
    <full file contents>
    === END FILE ===

Both blocks REQUIRE their closing marker (`=== END EDIT ===` / `=== END FILE ===`);
a truncated block does not parse. The newline directly before a closing marker is
framing, not content — so a FILE that should end WITH a trailing newline must have a
blank line before `=== END FILE ===`; without it the file ends with no newline.

Apply rules:
  - SEARCH text must match EXACTLY ONCE in the current file; 0 or >1 → reject
    with a structured failure so the caller can re-prompt for more context.
  - Line endings are normalized for matching; the file's existing EOL style and
    final-newline state are preserved on write.
  - Multiple edits to the same file compound in order.
  - apply_edits([]) returns ok=True (no ops, no failures). Detecting "Codex
    produced no parsable edits" is the Code phase's responsibility, not this
    module's — it treats an empty parse as a failure and re-prompts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .file_writer import safe_resolve, UnsafePathError


@dataclass
class EditOp:
    kind: str            # "edit" | "file"
    path: str
    search: str = ""     # EDIT only
    replace: str = ""    # EDIT only
    content: str = ""    # FILE only


@dataclass
class EditFailure:
    path: str
    reason: str
    match_count: int = -1   # -1 = not applicable; else actual SEARCH match count


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)      # relative paths written
    failures: list[EditFailure] = field(default_factory=list)
    ok: bool = True


# Both blocks REQUIRE their closing marker — a truncated block must not parse.
# The newline immediately before each closing marker is framing, not content;
# content/search/replace ending in a newline must include a blank line there.
_EDIT_RE = re.compile(
    r"===\s*EDIT:\s*(?P<path>[^\n=]+?)\s*===[ \t]*\n"
    r"<{5,}\s*SEARCH[ \t]*\n"
    r"(?P<search>.*?)\n"
    r"={5,}[ \t]*\n"
    r"(?P<replace>.*?)\n"
    r">{5,}\s*REPLACE[ \t]*\n"
    r"===\s*END\s*EDIT\s*===",
    re.DOTALL,
)

_FILE_RE = re.compile(
    r"===\s*FILE:\s*(?P<path>[^\n=]+?)\s*===[ \t]*\n"
    r"(?P<content>.*?)\n"
    r"===\s*END\s*FILE\s*===",
    re.DOTALL,
)


def _normalize(s: str) -> str:
    """Collapse CRLF/CR to LF for matching/storage."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _detect_eol(raw: str) -> str:
    return "\r\n" if "\r\n" in raw else "\n"


def parse_edits(text: str) -> list[EditOp]:
    """Parse EDIT and FILE blocks from `text`, preserving document order."""
    found: list[tuple[int, EditOp]] = []

    for m in _EDIT_RE.finditer(text):
        found.append((m.start(), EditOp(
            kind="edit",
            path=m.group("path").strip(),
            search=m.group("search"),
            replace=m.group("replace"),
        )))
    for m in _FILE_RE.finditer(text):
        found.append((m.start(), EditOp(
            kind="file",
            path=m.group("path").strip(),
            content=m.group("content"),
        )))

    found.sort(key=lambda t: t[0])
    return [op for _, op in found]


def apply_edits(ops: list[EditOp], target_dir: Path) -> ApplyResult:
    """Validate all ops, then write. Transactional: any failure ⇒ nothing written.

    Returns an ApplyResult; on failure, `failures` explains each rejection so the
    caller can re-prompt Codex (e.g. "SEARCH matched 0x — add more context").
    """
    failures: list[EditFailure] = []
    pending: dict[Path, tuple[str, str]] = {}   # resolved -> (content_lf, eol)
    applied_order: list[str] = []

    def _record_applied(rel: str) -> None:
        if rel not in applied_order:
            applied_order.append(rel)

    for op in ops:
        try:
            resolved = safe_resolve(target_dir, op.path)
        except UnsafePathError as e:
            failures.append(EditFailure(op.path, f"unsafe path: {e}"))
            continue

        if op.kind == "file":
            if resolved in pending:
                eol = pending[resolved][1]
            elif resolved.exists():
                eol = _detect_eol(resolved.read_bytes().decode("utf-8", "replace"))
            else:
                eol = "\n"
            pending[resolved] = (_normalize(op.content), eol)
            _record_applied(op.path)
            continue

        # --- EDIT ---
        if not op.search.strip():
            failures.append(EditFailure(op.path, "empty SEARCH block", 0))
            continue

        if resolved in pending:
            working, eol = pending[resolved]
        elif resolved.exists():
            raw = resolved.read_bytes().decode("utf-8", "replace")
            working, eol = _normalize(raw), _detect_eol(raw)
        else:
            failures.append(EditFailure(op.path, "file not found for EDIT", 0))
            continue

        search_lf = _normalize(op.search)
        count = working.count(search_lf)
        if count != 1:
            failures.append(EditFailure(
                op.path, f"SEARCH matched {count}x (need exactly 1)", count))
            continue

        new_working = working.replace(search_lf, _normalize(op.replace), 1)
        pending[resolved] = (new_working, eol)
        _record_applied(op.path)

    if failures:
        return ApplyResult(applied=[], failures=failures, ok=False)

    # Validation passed for all ops — write. (Not a write-time rollback: an OS
    # error mid-loop could leave a partial set; see file_writer's same caveat.)
    for resolved, (content_lf, eol) in pending.items():
        resolved.parent.mkdir(parents=True, exist_ok=True)
        out = content_lf.replace("\n", eol)
        resolved.write_bytes(out.encode("utf-8"))

    return ApplyResult(applied=applied_order, failures=[], ok=True)
