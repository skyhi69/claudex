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
    # Usage telemetry (Wave 1.3). Subscription quota/efficiency gauge — NOT dollars.
    # Zero when the CLI did not report usage.
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(ABC):
    """Abstract base for CLI-based LLM providers."""

    def __init__(self) -> None:
        # Session quota ledger (Wave 1.4): every send() is tallied here so the
        # decision brief can show who carried the load. Quota/efficiency, not $.
        self.call_count = 0
        self.usage_totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'claude', 'codex')."""
        ...

    def send(self, prompt: str, system_prompt: str = "", **kwargs) -> LLMResponse:
        """Send a prompt and return the response, recording usage for the ledger."""
        resp = self._send(prompt, system_prompt, **kwargs)
        self.call_count += 1
        self.usage_totals["input_tokens"] += getattr(resp, "input_tokens", 0) or 0
        self.usage_totals["cached_input_tokens"] += getattr(resp, "cached_input_tokens", 0) or 0
        self.usage_totals["output_tokens"] += getattr(resp, "output_tokens", 0) or 0
        return resp

    @abstractmethod
    def _send(self, prompt: str, system_prompt: str = "") -> LLMResponse:
        """Provider-specific send implementation (wrapped by send())."""
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
