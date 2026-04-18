"""Codex CLI provider — uses codex exec with Plus subscription."""

import subprocess
from .base import LLMProvider, LLMResponse


class CodexProvider(LLMProvider):
    """Wraps the OpenAI Codex CLI in non-interactive exec mode."""

    @property
    def name(self) -> str:
        return "codex"

    def _cli_command(self) -> str:
        return "codex"

    def send(self, prompt: str, system_prompt: str = "") -> LLMResponse:
        """Send a prompt via codex exec and capture the response.

        codex exec runs non-interactively: sends prompt, gets response, exits.
        """
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        cmd = [
            "codex", "exec",
            full_prompt,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min max per call
            )

            if result.returncode != 0:
                return LLMResponse(
                    content="",
                    provider=self.name,
                    success=False,
                    error=f"codex exec failed (exit {result.returncode}): {result.stderr.strip()}",
                )

            return LLMResponse(
                content=result.stdout.strip(),
                provider=self.name,
                success=True,
            )

        except subprocess.TimeoutExpired:
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error="Codex CLI timed out after 5 minutes",
            )
        except FileNotFoundError:
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error="Codex CLI not found. Is codex installed? Run: npm install -g @openai/codex",
            )
