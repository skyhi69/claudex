"""Verification detection + capture (Wave 2A).

Claudex — NOT Codex — runs the project's tests in the worktree, so the editor
can't certify its own work. Detection order (first hit wins):

  1. configured `test_command` (config.yaml)
  2. pytest      — tests/ dir, test_*.py / *_test.py, or pytest config
  3. npm test    — package.json with a "test" script
  4. smoke gate  — py_compile all .py / `node --check` all .js (greenfield/no tests)

The smoke gate proves "it compiles/imports", NOT "tests pass" — callers must
label it as such (D3) so a green smoke check is never reported as a test pass.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", "env", ".venv", ".serena",
    ".claudex", ".pytest_cache", ".mypy_cache", ".tox", "build", "dist", ".idea", ".vscode",
}
_MAX_FILES = 500
_OUTPUT_BUDGET = 8000   # chars of captured output kept (head + tail)


@dataclass
class VerificationResult:
    kind: str          # configured | pytest | npm | smoke | none
    command: str       # display string
    exit_code: int
    output: str        # combined stdout+stderr, truncated
    passed: bool
    is_smoke: bool     # True ⇒ "smoke check passed", NOT "tests passed" (D3)

    def label(self) -> str:
        if self.kind == "none":
            return "no verification applicable"
        verb = "smoke check passed" if self.is_smoke else "tests passed"
        return verb if self.passed else (
            "smoke check FAILED" if self.is_smoke else "tests FAILED")


def _iter_files(stage: Path, exts: tuple[str, ...]) -> list[str]:
    """Relative paths (posix-style) of files with the given extensions."""
    found: list[str] = []
    for path in stage.rglob("*"):
        if len(found) >= _MAX_FILES:
            break
        # Skip only the explicit junk set — do NOT blanket-skip all dotted dirs
        # (e.g. .github/scripts can hold real source worth smoke-checking).
        if any(part in _SKIP_DIRS for part in path.relative_to(stage).parts[:-1]):
            continue
        if path.is_file() and path.suffix in exts:
            found.append(path.relative_to(stage).as_posix())
    return found


def _has_pytest(stage: Path) -> bool:
    # Require ACTUAL tests. Config that merely mentions pytest is NOT enough — an
    # empty suite makes pytest exit 5, which would falsely fail real code. Use the
    # configured test_command override for non-standard test layouts.
    if (stage / "tests").is_dir() or (stage / "test").is_dir():
        return True
    for rel in _iter_files(stage, (".py",)):
        name = Path(rel).name
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
    return False


def _has_npm_test(stage: Path) -> bool:
    pkg = stage / "package.json"
    if not pkg.exists():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return False
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    return isinstance(scripts, dict) and bool(scripts.get("test"))


def _smoke_commands(stage: Path) -> list[list[str]]:
    py = _iter_files(stage, (".py",))
    if py:
        return [["python", "-m", "py_compile", *py]]
    js = _iter_files(stage, (".js", ".mjs", ".cjs"))
    if js:
        return [["node", "--check", f] for f in js]
    return []


def detect_verification(stage, configured: str = "") -> tuple[list[list[str]], str, bool]:
    """Return (commands, kind, is_smoke). commands run in order; all must pass.

    `configured` is parsed with shlex (POSIX rules): keep it a simple, shell-free
    command line. On Windows, prefer forward slashes in any paths and avoid
    backslash-quoting — wrap anything more complex in a script and point at that.
    """
    stage = Path(stage)
    if configured.strip():
        return [shlex.split(configured)], "configured", False
    if _has_pytest(stage):
        return [["python", "-m", "pytest", "-q"]], "pytest", False
    if _has_npm_test(stage):
        return [["npm", "test"]], "npm", False
    return _smoke_commands(stage), "smoke", True


def _truncate(text: str) -> str:
    if len(text) <= _OUTPUT_BUDGET:
        return text
    head = text[: _OUTPUT_BUDGET * 5 // 8]
    tail = text[-_OUTPUT_BUDGET * 3 // 8:]
    return f"{head}\n\n... [output truncated, {len(text)} chars total] ...\n\n{tail}"


def run_verification(stage, configured: str = "", timeout: int = 600) -> VerificationResult:
    """Detect and run verification in `stage`, capturing evidence for the audit."""
    commands, kind, is_smoke = detect_verification(stage, configured)

    if not commands:
        return VerificationResult(
            kind="none", command="", exit_code=0,
            output="(no verifiable files found — nothing to compile/test)",
            passed=True, is_smoke=False)

    chunks: list[str] = []
    passed = True
    last_code = 0
    for cmd in commands:
        exe = shutil.which(cmd[0]) or cmd[0]
        display = " ".join(cmd)
        try:
            proc = subprocess.run(
                [exe, *cmd[1:]], cwd=str(stage),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            )
        except FileNotFoundError:
            chunks.append(f"$ {display}\n[command not found: {cmd[0]}]")
            passed, last_code = False, 127
            continue
        except subprocess.TimeoutExpired:
            chunks.append(f"$ {display}\n[timed out after {timeout}s]")
            passed, last_code = False, -1
            continue
        out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
        chunks.append(f"$ {display}  (exit {proc.returncode})\n{out}".rstrip())
        last_code = proc.returncode
        # pytest exit 5 = "no tests collected" — not a code failure; treat as pass.
        if proc.returncode != 0 and not (kind == "pytest" and proc.returncode == 5):
            passed = False

    return VerificationResult(
        kind=kind,
        command="; ".join(" ".join(c) for c in commands),
        exit_code=0 if passed else (last_code or 1),
        output=_truncate("\n\n".join(chunks)),
        passed=passed,
        is_smoke=is_smoke,
    )
