# Claudex Deep-Dive Review — 2026-05-31

Architectural review of claudex against the current state-of-the-art in multi-agent
coding orchestration. Goal: become the "ultimate coding team" (Claude oversees, Codex
does cheap heavy lifting) while maximizing bang-for-buck.

---

## TL;DR — Where you stand

Your **core thesis is correct and SOTA-validated.** Aider's architect/editor benchmark
proved that "strong reasoner plans + cheaper model edits" gives *higher quality at ~14×
lower cost* than a single frontier model (R1+Sonnet: 64% @ $13 vs o1-alone: 62% @ $186,
[aider.chat](https://aider.chat/2025/01/24/r1-sonnet.html)). So the bet behind claudex is
sound.

But the current implementation **undercuts its own thesis in one decisive way**: Codex is
used as a *one-shot text generator*, not as the *agentic developer* it actually is. That
single design choice cascades into most of the problems below — no real codebase grounding,
no test execution, full-file regeneration instead of diffs, and the most expensive/least
reliable way to use the cheap model.

The good news: the skeleton (phase state machine, blind audit, consensus/stall detection,
memory, Windows-safe transport) is clean and worth keeping. The fixes are mostly about
*how the two CLIs are driven*, not a rewrite.

**Rank of impact:** (1) Use Codex agentically, (2) Track cost + route by complexity,
(3) Verify by running, (4) Structured output instead of regex, (5) Diff-based audit.

---

## ⚠️ UPDATE 2026-05-31 (post-probe) — two corrections that supersede parts of this doc

After live-probing `codex exec` on this Windows machine and a Claude↔Codex design review,
two things below are now **corrected**. Where the original text conflicts, this section wins.

### Correction A — "cost" framing was wrong: it's subscription *quota*, not dollars
Claudex shells out to `claude -p` and `codex exec`, both authenticated via **subscriptions**
(Claude Pro, ChatGPT/Codex) — **no API keys, no per-token dollar billing.** The
"$10 session / $5 task circuit breakers" referenced in finding #2 come from the global
CLAUDE.md and are an **API-cost concept that does not apply here.** The real scarce resources
are: **(1) rate-limit quota** (each plan caps usage per rolling window — exhaust it and you're
locked out mid-task), **(2) wall-clock time**, **(3) plan asymmetry** (Codex's plan gives more
heavy-lift headroom than Claude Pro's tighter quota). So "bang for the buck" = *don't spend
scarce Claude quota on work Codex can do.* Read the JSONL `usage` block as an **efficiency /
quota-pressure gauge**, not a billing meter. Everywhere this doc says "cost/dollars/$caps,"
read "Claude-call count / quota pressure."

### Correction B — finding #1's fix ("Codex edits a worktree autonomously") does NOT work on this machine
The original recommendation (and Codex's first review) was to run Codex agentically with
`--sandbox workspace-write` and let it edit a git worktree. **Live probing proved this is
blocked here.** Observed, headless `codex exec` (codex-cli 0.135.0, Windows 11):

| Capability | Result |
|---|---|
| Generate text / reason | ✅ always |
| **Read** real repo files (`--sandbox read-only`) | ✅ yes (read cmds auto-approved; occasional transient `windows sandbox: spawn setup refresh` → needs one retry) |
| **Write / edit** files (`--sandbox workspace-write`) | ❌ **declined** — `"file change approval is not supported in exec mode"` |
| Run mutating commands / most test suites | ❌ declined — same approval wall |
| `--json` `usage` telemetry | ✅ clean; ~38k/51k tokens *cached* (auto prompt-caching active) |

**Root cause:** Codex's OS sandbox is Linux/macOS-only. On Windows there's no sandbox, so every
mutation escalates to approval — and headless `exec` mode cannot grant approval. Your org policy
at `C:\ProgramData\OpenAI\Codex\requirements.toml` additionally blocks `approval_policy=Never`
and `danger-full-access` (the only settings that skip approval). `--dangerously-bypass-...`
would defeat an admin-installed security policy and is **not** recommended.

### Revised architecture (Wave 2A — Windows-native, viable today)
```
Codex (read-only)  → reads the REAL codebase, emits search/replace edits as TEXT   [grounding ✓]
Claudex (hands)    → validates paths, applies edits to a throwaway git worktree, RUNS tests   [verify ✓ + trust ✓]
Claude (read-only) → audits the real git diff + captured test output                [evidence ✓]
   ↑ loop on failure (existing resolve loop)
```
This is arguably *better* than autonomous-Codex-writes: grounding is solved (Codex reads real
files), and the editor never certifies its own work (claudex runs tests, so no model can
fake-green). **Edit format:** search/replace blocks (apply only on exactly-one exact match;
zero/multiple → reject + ask Codex to regenerate with more context), full-file fallback for
new/small files. The `git diff` is produced *after* apply, for Claude/user review — not for
applying. Linux/WSL2/VPS autonomous-write is a possible later "Wave 2B" backend, but only if
the guardrails are deliberately recreated there (else it bypasses the managed Windows policy).

**Team model:** debate the design together (Plan) → Codex implements grounded & solo (saves
Claude quota) → claudex verifies → Claude judges → loop together only on failure (Resolve).
Collaboration where it adds value; clean handoff where it doesn't.

---

## How it works today (verified from source)

```
INIT → ANALYZE → PLAN → CODE → AUDIT → (RESOLVE loop) → DONE
```

- **ANALYZE** (`phases/analyze.py`): Claude summarizes task, keyword-detects expertise,
  scans a *file list* of the target dir (not contents). Computes complexity but never uses it.
- **PLAN** (`phases/plan.py`): Claude↔Codex debate up to 10 rounds; consensus via a JSON
  block; rolling summaries trim history. Output = `final_plan` string, or falls back to the
  raw debate transcript.
- **CODE** (`phases/code.py`): Codex is asked to emit files in a custom
  `=== FILE: path ===` text format. **claudex regex-parses that text and writes the files
  itself** (`file_writer.py`).
- **AUDIT** (`phases/audit.py`): Claude reads *full file contents inline*, returns
  `VERDICT: APPROVED/REJECTED` parsed by regex.
- **RESOLVE** (`phases/resolve.py`): Claude→Codex discuss, Codex regenerates whole files,
  Claude re-audits. Up to 5 iterations.

Providers (`providers/claude.py`, `codex.py`): both write the prompt to a UTF-8 temp file,
pipe via stdin (good Windows fix), 15-min timeout. Codex runs
`codex exec --ephemeral -o out.txt -C <dir> -` — **no `--sandbox` flag passed (relies on
default config), used as a one-shot text generator: claudex parses the text and writes files
itself, so Codex never edits or runs anything.**

---

## Critical findings (ranked by impact)

### 1. Codex is neutered to a text generator — this is the big one
**What:** `codex exec --ephemeral -o out.txt` captures one blob of text. claudex then
regex-extracts `=== FILE ===` blocks and writes them via `file_writer.write_files`.

**Why it matters:**
- `codex exec` is an **agentic** tool. With `--sandbox workspace-write` it reads the real
  codebase, runs commands, runs tests, and edits files directly — iterating until it works.
  You're throwing all of that away and using it in its most expensive, least reliable mode:
  one-shot full-file generation *from imagination*.
- **No real grounding.** Codex never sees actual file contents — only the file *list* from
  `_scan_project` + a `project_context` blurb. Any change to an existing codebase is the
  model guessing the surrounding code. This is the textbook hallucinated-API / wrong-signature
  failure mode the research flags.
- **No diffs.** Every "modify" is `action="create"` (full overwrite). Codex must regenerate
  entire files every iteration — maximal token burn and high risk of dropping unrelated code.
- **Fragile capture.** If Codex doesn't use the exact `=== FILE ===` wrapper, fallbacks
  *guess* filenames and even guess `.py` vs `.js` from content (`code.py:144-153`). Silent
  mis-saves are possible.

**SOTA backing:** OpenHands/Codex/Aider all let the executor *act* in a sandbox and verify.
Aider's lesson is specifically: reasoner emits prose, editor emits **real edits** — not that
the editor re-emits whole files blind.

**Fix:** Drive Codex agentically. Run it inside the target git repo with
`--sandbox workspace-write`, hand it the plan, and let it read/edit/run. Capture the **git
diff** as the artifact instead of parsing a custom text format. claudex stops being the file
writer; it becomes the orchestrator + reviewer. (Keep a backup/branch for safety.)

---

### 2. Zero usage tracking — your #1 goal is unmeasured
> **See Correction A above:** this is about subscription **quota / call-pressure**, not dollars.
> Read "cost/$caps" below as "Claude-quota pressure / call budget."

**What:** No token or usage accounting anywhere. `claude -p --output-format json` and
codex's output **both carry `usage` metrics**, but `_parse_output` discards everything except
the text.

**Why it matters:** "Bang for the buck" is the stated objective and it's currently
invisible. The CLAUDE.md circuit breakers ($10 session / $5 task / 10-min agent) exist on
paper but **nothing enforces them.** You can't optimize what you don't measure.

**Fix:**
- Parse `usage` (input/output/cache tokens) from both CLIs; attach per-call **token usage**
  to `LLMResponse`. Codex `--json` emits JSONL `usage` events; `claude -p --output-format json`
  returns token counts. **Track calls / tokens / cache-hit / wall-time only — never dollars**
  (Correction A: subscription quota, not metered billing).
- Accumulate per-phase and per-session totals into `SessionState`; print a cost line in the
  decision brief; abort when a configured cap is hit.
- This also unlocks honest A/B: paired vs Claude-solo vs Codex-solo on *your* tasks (Aider's
  caveat: with two frontier models the pairing gain can vanish — measure it).

---

### 3. Complexity is computed but never used — no routing/tiering
**What:** `analyze` returns `complexity` ∈ {simple, moderate, complex}. It's displayed and
saved, then **ignored.** A one-line "simple" task runs the same 10-round debate + audit +
resolve as a complex one.

**Why it matters:** Model tiering/routing is the single biggest documented cost lever
(40–70% savings; 60–80% of coding requests are routine). Running the full heavyweight
pipeline on trivial tasks is pure waste — and ironically spends Claude (the expensive side)
on debates that don't need them.

**Fix:** Route on complexity:
- *simple* → skip the debate; Claude writes a 3-line spec, Codex implements, single audit.
- *moderate* → 2–3 round plan, normal audit.
- *complex* → full pipeline.
Optionally pick model strength per phase (e.g. a cheaper Claude tier for analyze).

---

### 4. Nothing ever runs the code — "approved" = "Claude read it and liked it"
**What:** The only gate is Claude's text review producing `VERDICT: APPROVED`. No tests, no
execution, no `python -c`, no import check.

**Why it matters:** This is the exact anti-pattern the research and *your own CLAUDE.md*
warn against: **"Done = ran and observed, never inspected."** Reading code != running it.
Silent failure (agent reports success without verification) is the most common production
failure mode.

**Fix:** Add a **verification step** between CODE and AUDIT:
- Have Codex (in its sandbox) run the project's tests / a smoke import / `python -c`.
- Capture stdout + exit code as evidence and feed *that* into the audit.
- Best: TDD ordering — generate/confirm a failing test first, implement, prove it green.
  Make a red→green transition the approval criterion, not vibes.

---

### 5. Brittle regex parsing where structured output exists
**What:** Consensus JSON blocks, `VERDICT:` detection, `=== FILE ===` extraction, and
`_extract_issues` (keyword regex) are all fragile. `_extract_issues` in particular matches
*any* line containing "issue/problem/bug/concern" and manufactures `AuditIssue`s from prose —
low signal.

**Why it matters:** Parsing failures silently corrupt control flow (wrong verdict, dropped
files, phantom issues). Both CLIs support enforced schemas: Codex `--output-schema schema.json`,
Claude `--output-format json --json-schema`.

**Fix:** Use enforced JSON schemas for the audit verdict (approved + structured issue list)
and the consensus block. Keep regex only as a fallback. Delete the keyword issue-scraper.

---

### 6. Audit dumps full file contents to the expensive model
**What:** `audit.py` inlines every file's entire content into Claude's prompt.

**Why it matters:** Claude is the costly side; full-file review is ~10× the tokens of a
diff-based review and gets worse as the codebase grows (lost-in-the-middle: models underweight
the middle of long contexts). For modifications you only need the *diff* + a repo-map.

**Fix:** Review the **git diff** (plus targeted symbol windows), not whole files. Put the
plan + acceptance criteria at the *top* of the prompt and the diff at the *bottom* (caching +
attention both favor this).

---

### 7. The "plan" handed to Codex may be a debate transcript, not a spec
**What:** `agreed_plan = final_plan if final_plan else _trim_history(rounds)`. If no clean
`final_plan` JSON field was captured, Codex receives the **raw back-and-forth** as its plan.

**Why it matters:** Aider's hard-won lesson: pass the architect's *clean final output*, never
its scratchpad — forwarding reasoning/transcript made results *worse*. A messy transcript as
the implementation spec invites misimplementation.

**Fix:** Always synthesize a clean plan: if no `final_plan` block exists at consensus, do one
final Claude call "write the agreed implementation plan as a concrete spec." Never ship the
transcript as the plan.

---

### 8. No live output during multi-minute calls
**What:** `subprocess.run` blocks for up to 15 min; `on_message` only fires *after* the full
response returns. "Verbose" mode shows nothing while Codex works.

**Fix:** Use Codex `--json` / Claude streaming and pump events to `on_message` as they arrive.
Bonus: stream gives you live `usage` for the cost breakers in #2.

---

### 9. Planning & analysis are semi-blind to the real code
**What:** Both phases see only a 50-file *name list* + optional `.claudex/context.md`. No file
contents, no repo-map (symbol signatures / dependency edges).

**Why it matters:** Fine for greenfield, weak for modifying existing projects — the model
plans against names it's guessing the contents of.

**Fix:** Build a lightweight **repo-map** (top symbols per file via tree-sitter or even ctags)
and include it in analyze/plan. ~10× cheaper than file dumps, gives real architectural
visibility. Let the agentic Codex (finding #1) read specific files on demand.

---

## Smaller issues / polish
- **Codex sandbox not set explicitly** (`codex.py:53`): relies on default. Set `--sandbox`
  deliberately per the desired mode.
- **`require_approval` + agentic edits**: if Codex edits files directly (finding #1), guard
  with a git branch/stash so the user still gets a reviewable diff before merge.
- **Hardcoded `~/Claude_Projects`** (`cli.py:30`): fine for you, but config-driven would help.
- **`max_tokens*` in `PhaseConfig` are dead** — defined, never threaded into provider calls.
- **No resume/caching of Codex sessions across CODE→RESOLVE**: `codex exec resume` could carry
  context instead of re-sending the full plan + previous code each iteration.
- **Stall/consensus tuning**: `agreed=true` is cheap to emit; with weak grounding the debate
  can "agree" on a vague plan. Tighten by requiring the final_plan block at agreement.

---

## What's already good (keep it)
- Clean phase state machine + dataclass models — easy to extend.
- **Blind audit** by the *other* model — correct anti-sycophancy structure.
- Consensus + stall detection with rolling summaries — solid context-rot mitigation in PLAN.
- Windows-safe temp-file/stdin transport (the in-progress diff) — real bug fixes.
- Memory: relevance-scored lessons, dedupe, severity-gated auto-learn — lightweight and sane.
- No API keys / SDKs — rides existing CLI auth. Genuinely low-friction.

---

## Suggested roadmap (cheapest-first)

**Phase A — Measure & route (low effort, high leverage)**
1. Parse `usage` from both CLIs → per-phase/session cost in the brief + enforce caps.
2. Use `complexity` to route: simple → fast path, complex → full pipeline.

**Phase B — Make Codex a real developer (the big structural win)** — *revised by Correction B*
3. ~~Run Codex agentically with `--sandbox workspace-write`~~ **(blocked on Windows — see
   Correction B).** Instead: run Codex **read-only & grounded**, emit search/replace edits;
   **claudex** validates paths + applies to a worktree; capture **git diff** after apply.
4. Add a verification step: **claudex** runs tests / smoke-run; feed evidence into audit.
5. Switch audit to **diff-based** review + enforced JSON verdict schema.

**Phase C — Robustness & polish**
6. Enforced schemas for consensus + verdict; delete keyword issue-scraper.
7. Always synthesize a clean plan spec (never ship the transcript).
8. Streaming output + live cost.
9. Repo-map for grounding.

---

## Key sources
- Aider architect/editor mechanics & benchmarks:
  https://aider.chat/2024/09/26/architect.html · https://aider.chat/2025/01/24/r1-sonnet.html
- Anthropic, Building Effective Agents (orchestrator-worker, evaluator-optimizer):
  https://resources.anthropic.com/building-effective-ai-agents
- Codex non-interactive (flags, JSONL usage, sandbox, resume):
  https://developers.openai.com/codex/noninteractive
- Claude headless (`-p`, json output, schema):
  https://code.claude.com/docs/en/headless
- Verify-by-running patterns (Simon Willison):
  https://simonwillison.net/guides/agentic-engineering-patterns/agentic-manual-testing/
- Cost levers (tiering/caching/compaction/diff-context): https://www.morphllm.com/llm-cost-optimization
