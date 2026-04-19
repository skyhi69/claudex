"""Base provider interface for LLM CLI wrappers."""

from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class LLMResponse:
    """Parsed response from an LLM provider."""
    content: str
    provider: str       # "claude" or "codex"
    success: bool
    error: str = ""


class LLMProvider(ABC):
    """Abstract base for CLI-based LLM providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'claude', 'codex')."""
        ...

    @abstractmethod
    def send(self, prompt: str, system_prompt: str = "") -> LLMResponse:
        """Send a prompt and return the response."""
        ...

    def is_available(self) -> bool:
        """Check if the CLI tool is installed and accessible."""
        import subprocess
        import shutil
        # First check if the command exists on PATH
        if shutil.which(self._cli_command()) is None:
            return False
        try:
            result = subprocess.run(
                [self._cli_command(), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    @abstractmethod
    def _cli_command(self) -> str:
        """The CLI executable name."""
        ...
