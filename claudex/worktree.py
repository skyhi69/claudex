"""Git worktree lifecycle for the grounded-build flow (Wave 2A).

Claudex stages Codex's edits in a throwaway git worktree, runs the tests there,
captures the diff for Claude's audit, and only on approval applies that exact
tested diff back to the real project (D1: git-native `git apply`, not file copy).
The user's working tree is never touched until they approve.

Flow:
    inited = ensure_repo(target)                  # greenfield → git init + commit
    stage  = create_worktree(target, session_id)  # checkout on claudex/<id> branch
    # ... apply edits into `stage`, run tests there ...
    patch  = stage_diff(stage)                     # the tested artifact (binary-safe)
    names  = stage_name_status(stage)              # human-readable changed files
    # ... Claude audits patch+test output; user approves ...
    commit_stage(stage)                            # preserve the proposal on claudex/<id>
    apply_patch(target, patch)                     # WORKING-TREE apply; refuses if dirty
    remove_worktree(target, stage)                 # always, in a finally
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# claudex's own commit identity, applied via -c so it works without global config.
_IDENT = ["-c", "user.email=claudex@local", "-c", "user.name=claudex"]


class GitError(Exception):
    """A git command failed."""


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def _git_bin() -> str:
    return shutil.which("git") or shutil.which("git.exe") or "git"


def git_available() -> bool:
    try:
        return _git(["--version"], cwd=None, check=False).returncode == 0
    except OSError:
        return False


def _git(args: list[str], cwd, check: bool = True, timeout: int = 120) -> GitResult:
    proc = subprocess.run(
        [_git_bin()] + args,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    res = GitResult(
        returncode=proc.returncode,
        stdout=proc.stdout.decode("utf-8", "replace"),
        stderr=proc.stderr.decode("utf-8", "replace"),
    )
    if check and res.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed (exit {res.returncode}): {res.stderr.strip()}")
    return res


def is_git_repo(path) -> bool:
    try:
        r = _git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _has_commit(path) -> bool:
    return _git(["rev-parse", "--verify", "HEAD"], cwd=path, check=False).returncode == 0


def ensure_repo(target, auto_git_init: bool = True, on_message=None) -> bool:
    """Ensure `target` is a git repo with at least one commit.

    Returns True if claudex initialized git here (greenfield). Raises GitError if
    the target is not a repo and auto_git_init is False.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)

    repo = is_git_repo(target)
    if repo and _has_commit(target):
        return False

    if not repo:
        if not auto_git_init:
            raise GitError(f"{target} is not a git repository and auto_git_init is disabled")
        _git(["init"], cwd=target)

    # Repo exists (new or pre-existing) but has no commit yet — make the baseline.
    _git(["add", "-A"], cwd=target, check=False)
    _git(_IDENT + ["commit", "-m", "claudex: initial snapshot", "--allow-empty"], cwd=target)
    if on_message and not repo:
        on_message("system", "Claudex", f"Initialized git in {target} so changes can be reviewed.")
    return not repo


def _sanitize_ref(token: str) -> str:
    """Make a git-ref-safe token from an arbitrary session id."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", token or "").strip("-.")
    return (cleaned or "session")[:64]


def create_worktree(target, session_id: str) -> Path:
    """Create a throwaway worktree checked out on a fresh claudex/<id> branch."""
    parent = Path(tempfile.mkdtemp(prefix="claudex_wt_"))
    stage = parent / "wt"   # must not pre-exist — git worktree add creates it
    branch = f"claudex/{_sanitize_ref(session_id)}"
    _git(["worktree", "add", "-b", branch, str(stage), "HEAD"], cwd=target)
    return stage


def commit_stage(stage, message: str = "claudex: proposed changes") -> bool:
    """Commit the worktree's staged changes to its branch (a real backup).

    Returns True if a commit was made, False if there was nothing to commit. Call
    this BEFORE remove_worktree if you want claudex/<id> to actually preserve the
    proposed changes — otherwise --force removal discards them and the branch
    stays at HEAD.
    """
    _stage_all(stage)
    staged = _git(["diff", "--cached", "--name-only"], cwd=stage, check=False).stdout.strip()
    if not staged:
        return False
    _git(_IDENT + ["commit", "-m", message], cwd=stage)
    return True


# Build artifacts that verification (pytest/py_compile) or tooling create in the
# worktree — must never enter the tested diff. We can't pass these as `git add`
# exclude pathspecs: an exclude combined with the base `.` pathspec makes git
# ERROR on paths already gitignored in an existing repo. Instead: plain `add`
# (respects the repo's .gitignore, never errors) then unstage these globs to
# cover the greenfield case where they are not gitignored.
_ARTIFACT_GLOBS = [
    "**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**",
    "**/.mypy_cache/**", ".serena/**",
]


def _stage_all(stage) -> None:
    _git(["add", "-A"], cwd=stage)
    _git(["reset", "-q", "--", *[f":(glob){g}" for g in _ARTIFACT_GLOBS]], cwd=stage, check=False)


def stage_diff(stage) -> str:
    """Stage all changes (excluding build artifacts) and return the binary-safe patch."""
    _stage_all(stage)
    return _git(["diff", "--cached", "--binary"], cwd=stage).stdout


def stage_name_status(stage) -> str:
    """Human-readable changed-file list (A/M/D + path) for the brief."""
    return _git(["diff", "--cached", "--name-status"], cwd=stage).stdout.strip()


def is_clean(target) -> bool:
    """True if the target working tree has no uncommitted changes."""
    r = _git(["status", "--porcelain"], cwd=target, check=False)
    return r.returncode == 0 and not r.stdout.strip()


def apply_patch(target, patch_text: str, require_clean: bool = True) -> bool:
    """Apply a tested patch to `target`'s WORKING TREE only. Returns True on success.

    Working-tree-only (no `--index`) so claudex never silently stages the user's
    files or collides with their pre-existing staged changes — they review with
    `git status` and stage/commit themselves. Tries `git apply --binary`, falling
    back to `--3way` if the base drifted. Empty patch is a no-op success.

    With require_clean=True (default) the apply is refused (GitError) if the
    target has uncommitted changes, so the tested patch can only land on the same
    state Claude reviewed.
    """
    if not patch_text.strip():
        return True

    if require_clean and not is_clean(target):
        raise GitError(
            "target has uncommitted changes; refusing to apply the tested patch "
            "onto a different state than was reviewed (commit/stash first)"
        )

    tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".patch", delete=False)
    try:
        tmp.write(patch_text.encode("utf-8"))
        tmp.close()
        r = _git(["apply", "--binary", tmp.name], cwd=target, check=False)
        if r.returncode != 0:
            r = _git(["apply", "--binary", "--3way", tmp.name], cwd=target, check=False)
        return r.returncode == 0
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def remove_worktree(target, stage) -> None:
    """Remove the worktree and its temp parent; prune stale entries. Never raises."""
    stage = Path(stage)
    _git(["worktree", "remove", "--force", str(stage)], cwd=target, check=False)
    shutil.rmtree(stage.parent, ignore_errors=True)
    _git(["worktree", "prune"], cwd=target, check=False)
