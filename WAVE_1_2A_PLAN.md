# Claudex — Wave 1 + Wave 2A Implementation Plan

**Status:** proposed, not implemented. For independent review (Codex) before any
`claudex/*.py` is touched.
**Date:** 2026-05-31
**Basis:** `DEEP_DIVE_REVIEW.md` (+ its post-probe UPDATE). Architecture validated by live
`codex exec` probing on Windows 11 / codex-cli 0.135.0.

---

## Goals

1. **Wave 1 — safety & testability:** stop unsafe writes, replace fragile prose parsing with
   structured contracts, add tests around the parsers/providers.
2. **Wave 2A — grounded build + evidence audit (Windows-native):**
   - Codex reads the **real** codebase (read-only) and emits **search/replace edits**.
   - Claudex validates paths and applies edits to a **throwaway git worktree**, then **runs
     the tests itself**.
   - Claude audits the **real git diff + test output** (evidence, not prose).
   - Capture **JSONL `usage`** from both CLIs into a per-session quota ledger.

## Non-goals (explicitly deferred)
- Linux/WSL2/VPS autonomous-write backend ("Wave 2B").
- Repo-map, benchmark harness, extra subroles.
- Streaming live output (nice-to-have; after 2A).

---

## Confirmed decisions
- **(a) Greenfield:** claudex auto-`git init`s empty/non-git targets so the worktree+diff flow
  is uniform.
- **(b) Edit format:** search/replace blocks primary; full-file fallback for new/small files or
  after repeated search/replace failure. `git diff` produced *after* apply, for review only.
- **Platform:** build Windows-native 2A first. No `--dangerously-bypass-*` (defeats managed
  policy). No WSL2 default.

## Resolved by Codex review (2026-05-31)
- **D1 — apply-on-approval mechanism → GIT-NATIVE PATCH (decided).** Codex review: file-copy can
  drift from what was tested (misses deletes, renames, file modes, binaries). **Default:** apply
  edits in the worktree → run tests → `git -C <wt> diff --binary` is the tested artifact → on
  approval, `git -C <target> apply --index <patch>` (faithful to exactly what passed), then show
  `git status`. **File-copy is fallback only** (e.g. if `git apply` rejects). Keep the
  `claudex/<session>` branch as a backup either way.
  - *Risk:* `git apply` can reject if the target working tree drifted from the worktree's base
    HEAD (uncommitted local changes). Mitigation: snapshot/verify target is clean against the
    worktree base before applying; if dirty, warn and fall back to a 3-way `git apply --3way`.

## Open decisions (need a call — flagged for review)
- **D2 — verdict transport:** does `claude -p` on this box support enforced `--json-schema`?
  If yes, use it for the audit verdict; if not, use a fenced-JSON verdict contract + robust
  parse. **Must verify live before relying** (recency rule). Fallback path always kept.
- **D3 — test detection default** when none is configured / greenfield (see Test detection).

---

## Wave 1 — Safety & testability

### 1.1 Path confinement (`file_writer.py`)
**Problem:** `target_dir / f.path` writes/deletes with zero validation — `..\..\x`, absolute
paths, drive-qualified (`C:\...`), and UNC (`\\host\share`) all escape `target_dir`.

**Add** a `safe_resolve(target_dir, rel)` gate, called before every write/delete:
```python
from pathlib import Path, PureWindowsPath

class UnsafePathError(Exception): ...

def safe_resolve(target_dir: Path, rel: str) -> Path:
    pw = PureWindowsPath(rel)
    if pw.is_absolute() or pw.drive or rel.startswith(("\\\\", "//", "/", "\\")):
        raise UnsafePathError(f"absolute/UNC/drive path rejected: {rel!r}")
    if ".." in pw.parts:
        raise UnsafePathError(f"parent traversal rejected: {rel!r}")
    base = target_dir.resolve()
    resolved = (base / rel).resolve()
    if not _is_within(resolved, base):
        raise UnsafePathError(f"escapes target dir: {rel!r}")
    return resolved

def _is_within(child: Path, base: Path) -> bool:
    try:                       # Path.is_relative_to is py3.9+
        return child.is_relative_to(base)
    except AttributeError:     # portability fallback for <3.9
        try:
            child.relative_to(base)
            return True
        except ValueError:
            return False
```
- On violation: skip that file, record a hard error in the write summary, and **fail the
  phase** (don't silently drop). Unsafe paths from a model are a signal, not a typo.
- Applies equally to the Wave-2A worktree apply step.

### 1.2 Structured contracts (reduce regex fragility)
- **Consensus block** (`consensus.py` / `roles.py`): already JSON — keep, but add a tiny schema
  validation (required keys: `agreed: bool`, `concerns: list`, `position: str`). Treat malformed
  as "block missing" (existing behavior).
- **Audit verdict** (`audit.py`): replace `_detect_approval` prose-scanning with a required
  fenced JSON verdict (see Wave 2A audit). Keep last-match regex only as fallback.
- **Delete `_extract_issues` keyword scraper** — it manufactures `AuditIssue`s from any line
  containing "issue/problem/bug". Replace with structured `issues: []` from the verdict JSON.

### 1.3 Tests (new `tests/` — currently only consensus is covered)
- `test_file_writer.py`: rejects `..`, absolute, `C:\`, UNC; accepts normal relative; backup
  behavior; delete-confinement.
- `test_edits.py`: search/replace parse + apply (exactly-one match, zero match, multi match,
  **CRLF vs LF**, **mixed line endings**, **no-final-newline / final-newline preservation**,
  fallback to full-file). EOL style of the original file must be preserved on write.
- `test_codex_provider.py`: JSONL `usage` parse; retry on `spawn setup refresh`; final-message
  extraction. (Mock subprocess — no live CLI in unit tests.)
- `test_audit.py`: verdict JSON parse (approved/rejected/malformed→reject).

---

## Wave 2A — Grounded build + evidence audit

### 2.1 Edit format (the contract Codex emits)
**Search/replace block** (Aider-style), one or more per response:
```
=== EDIT: relative/path/to/file.py ===
<<<<<<< SEARCH
<exact existing lines, verbatim, including indentation>
=======
<replacement lines>
>>>>>>> REPLACE
=== END EDIT ===
```
**New file / full replacement:**
```
=== FILE: relative/path/to/new_file.py ===
<full file contents>
=== END FILE ===
```
**Apply rules (`edits.py`, new module):**
1. `safe_resolve` the path first (Wave 1.1).
2. For `EDIT`: the SEARCH text must match **exactly once** in the current file.
   - 0 matches or >1 matches → **reject the edit**, return a structured failure
     (`path`, `reason`, `match_count`) so the Code phase can re-prompt Codex *"SEARCH block
     matched N times; regenerate with more surrounding context."*
   - Normalize line endings for matching (read file, match on `\n`), write back preserving the
     file's existing EOL style.
3. For `FILE`: full write (new file, or overwrite small file).
4. Apply is **transactional per response**: if any edit in a batch fails, apply none; report
   failures for re-prompt. (Avoids half-applied states.)

### 2.2 Worktree lifecycle (`worktree.py`, new module)
```
ensure_repo(target):        # D(a): greenfield → git init + initial commit IF config.auto_git_init
                            #   (default true). MUST print a visible notice:
                            #   "Claudex initialized git in <dir> so changes can be reviewed."
                            #   Changes project state pre-approval → make it explicit & opt-out-able.
stage = worktree_add(target, branch=f"claudex/{session_id}")   # detached checkout of HEAD
apply_edits(stage, edits)   # Wave 2.1, path-confined
test_out = run_tests(stage) # 2.3
diff = git_diff(stage)      # `git -C stage add -A && git -C stage diff --cached`
# → hand diff + test_out to audit (2.4)
# on approval: D1 (default copy changed files → target, keep claudex/<session> branch)
# always: worktree_remove(stage) in a finally
```
- All git via `git -C <dir>` (no `Set-Location`).
- Cleanup in `finally`; never leave dangling worktrees.

### 2.3 Test / verification detection (`run_tests`)
Detection order (first hit wins), overridable by `config.yaml: test_command`:
1. Explicit `config.yaml` `test_command`.
2. Python: `pytest` if `tests/`, `test_*.py`, or `[tool.pytest]` present.
3. Node: `npm test` if `package.json` has `scripts.test`.
4. **Greenfield / no tests (D3 default):** *smoke gate* — `py_compile` all `.py` /
   `node --check` all `.js`, or import the top module. Gate = "it compiles/imports," not
   "tests pass." Clearly labeled in the evidence as a smoke check, not a test pass.
- Capture stdout+stderr+exit code (truncate to a budget, keep head+tail). This is the evidence.
- **Claudex runs this — never Codex** (trust: the editor can't certify itself).

### 2.4 Evidence-based audit (`audit.py` rewrite)
- Input to Claude: original task + **the git diff** (not full files) + **test/smoke output +
  exit code** + the agreed plan (top of prompt; diff+evidence at bottom — caching & attention).
- Output contract (D2): fenced JSON
  ```json
  {"approved": true, "issues": [{"severity":"high","file":"x.py","issue":"...","fix":"..."}],
   "assessment": "one paragraph"}
  ```
  Parse JSON; fallback to `VERDICT:` regex if absent. **Block approval if test/smoke evidence is
  missing or failing**, regardless of prose (enforces "ran and observed").

### 2.5 Providers — usage ledger + Windows hardening
- `base.py LLMResponse`: add `input_tokens`, `cached_input_tokens`, `output_tokens`, `ok` usage
  fields (default 0).
- `codex.py`: add `--json` + `--sandbox read-only` (configurable) + keep `-o` for final message;
  parse JSONL for `turn.completed.usage`; **retry ≤2× on `windows sandbox: spawn setup refresh`
  and on empty output.** Keep temp-file stdin transport (works; arg-prompt hangs headless).
- `claude.py`: parse `usage`/cost fields from `--output-format json` into `LLMResponse`.
- `models.py`: `SessionState.usage: list[UsageEvent]` (phase, provider, tokens); helper to total
  per-provider. `DecisionBrief` gains a **quota line**: Claude calls/tokens vs Codex calls/tokens
  + cache-hit ratio. (Framed as quota/efficiency, not dollars — see Correction A.)
- Optional soft guard: warn (don't hard-abort) when Claude calls in a session exceed a
  configurable `quota_warn_threshold`.

### 2.6 Orchestrator / Code phase wiring
- `phases/code.py`: prompt Codex (read-only, in-repo) to emit the 2.1 edit format; on apply
  failures, re-prompt with the structured failure (≤ N retries) before giving up.
- `orchestrator._handle_code`: create worktree → apply → run tests → store diff+evidence on
  state. `_handle_audit`: feed diff+evidence. `resolve.py`: loop reuses the same
  apply→test→audit path (already structurally close).
- `cli.py`: approval step shows the **diff + test result + quota line**, then applies per D1.

---

## File-by-file change summary

| File | Wave | Change |
|---|---|---|
| `file_writer.py` | 1 | `safe_resolve` gate; fail-on-unsafe |
| `consensus.py` | 1 | light schema validation of consensus block |
| `audit.py` | 1+2A | drop keyword scraper; JSON verdict; **diff+evidence** input |
| `edits.py` *(new)* | 2A | parse + transactional apply of search/replace + full-file |
| `worktree.py` *(new)* | 2A | ensure_repo / add / diff / remove; greenfield git-init |
| `runner.py` *(new)* | 2A | test/smoke detection + capture |
| `providers/base.py` | 2A | usage fields on `LLMResponse` |
| `providers/codex.py` | 2A | `--json` + read-only + usage parse + retry |
| `providers/claude.py` | 2A | usage parse |
| `models.py` | 2A | usage ledger; diff/evidence on state; brief quota line |
| `phases/code.py` | 2A | edit-format prompt + re-prompt on apply failure |
| `orchestrator.py` | 2A | worktree→apply→test→audit wiring; apply-on-approval |
| `cli.py` | 2A | show diff+evidence+quota at approval |
| `config.py` | 2A | `test_command`, `codex_sandbox`, `quota_warn_threshold`, `auto_git_init` (default true) |
| `tests/*` *(new)* | 1 | parser/provider/path/audit unit tests |

---

## Rollout order (each step independently testable)
1. Wave 1.1 path confinement + tests. *(safety first — ships value alone)*
2. `edits.py` + tests (pure, no CLI).
3. Provider usage parse + retry + tests (mocked).
4. `worktree.py` + `runner.py` + tests.
5. Wire orchestrator/code/audit to the new flow.
6. CLI surface (diff + quota at approval).
7. End-to-end smoke on a throwaway project (greenfield + existing-repo paths).

## Risks / mitigations
- **`spawn setup refresh` transient** → bounded retry (proved to succeed on retry).
- **Search/replace non-unique matches** → reject + re-prompt with more context (Codex's rule).
- **`claude --json-schema` support unknown** (D2) → verify live; keep regex fallback.
- **Apply-on-approval** (D1, resolved) → **git-native `git apply --index`** of the tested
  `diff --binary`; file-copy only as fallback; `git apply --3way` if target drifted; keep
  `claudex/<session>` backup branch.
- **Greenfield git-init** changes project state pre-approval → gate behind `auto_git_init`
  (default true) **and** print a visible notice ("Claudex initialized git in <dir> ...").

---

## What success looks like
- No path can escape `target_dir`. 
- Codex's edits are grounded in real file contents; non-unique edits are caught, not mis-applied.
- Every "approved" carries captured test/smoke evidence Claude actually reviewed.
- The decision brief shows a quota line proving Codex carried the bulk and Claude was spent only
  on planning + judgment.
