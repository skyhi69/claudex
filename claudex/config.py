"""Configuration loader for Claudex."""

import yaml
from pathlib import Path
from dataclasses import dataclass


@dataclass
class PhaseConfig:
    max_rounds: int = 10
    max_iterations: int = 5
    max_rounds_per_iteration: int = 3
    max_tokens_per_turn: int = 2000
    max_tokens: int = 8000


@dataclass
class ClaudexConfig:
    planning_max_rounds: int = 10
    resolve_max_iterations: int = 5
    stall_threshold: int = 2
    backup_files: bool = True
    require_approval: bool = True
    # Wave 2A grounded build
    test_command: str = ""          # explicit verification command (overrides detection)
    auto_git_init: bool = True      # init git in greenfield targets so the worktree flow works


def load_config(config_path: Path | None = None) -> ClaudexConfig:
    """Load configuration from YAML file, with defaults."""
    defaults = ClaudexConfig()

    if config_path and config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        caps = data.get("safety_caps", {})
        output = data.get("output", {})
        build = data.get("build", {})

        return ClaudexConfig(
            planning_max_rounds=caps.get("max_planning_rounds", defaults.planning_max_rounds),
            resolve_max_iterations=caps.get("max_resolve_iterations", defaults.resolve_max_iterations),
            stall_threshold=caps.get("stall_threshold", defaults.stall_threshold),
            backup_files=output.get("backup_files", defaults.backup_files),
            require_approval=output.get("require_approval", defaults.require_approval),
            test_command=build.get("test_command", defaults.test_command),
            auto_git_init=build.get("auto_git_init", defaults.auto_git_init),
        )

    return defaults
