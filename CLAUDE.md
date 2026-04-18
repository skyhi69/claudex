# Claudex

Multi-agent coding orchestrator: Claude (Architect) + Codex (Developer) collaborate autonomously.

## Architecture
- **Python CLI** — run via `python -m claudex "task description" --dir ./project`
- **6-phase pipeline**: Analyze → Plan → Code → Audit → Resolve → Done
- **CLI-based providers**: `claude -p` (Pro subscription) and `codex exec` (Plus subscription)
- **No API keys required** — uses existing CLI authentication

## Key Design Principles
- Claude leads as Technical Architect, Codex implements as Senior Developer
- Anti-sycophancy: structural (blind audit, thorough eval), not just prompts
- Anti-hallucination: grounding rule, no pseudocode, explicit uncertainty
- Dynamic rounds: continue until consensus or stall, generous safety caps
- Expert roles assigned dynamically per task

## Project Structure
```
claudex/
├── cli.py           — Entry point, argparse + rich output
├── orchestrator.py  — State machine (graph-based, from Expert Council pattern)
├── models.py        — Data models (SessionState, DebateRound, etc.)
├── config.py        — YAML config loader
├── providers/       — CLI wrappers (claude.py, codex.py)
├── phases/          — Pipeline phases (analyze, plan, code, audit, resolve)
├── consensus.py     — Agreement + stall detection
├── roles.py         — Dynamic expert role builder
└── file_writer.py   — Safe file writes with backups
```

## Dependencies
- pyyaml, rich (that's it — no AI SDKs)
- Claude Code CLI (authenticated)
- Codex CLI (authenticated)
