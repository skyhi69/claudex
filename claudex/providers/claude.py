"""Claude Code CLI provider — uses claude -p (pipe mode) with Pro subscription."""

import subprocess
import json
from .base import LLMProvider, LLMResponse


class ClaudeProvider(LLMProvider):
    """Wraps the Claude Code CLI in non-interactive pipe mode."""

    @property
    def name(self) -> str:
        return "claude"

    def _cli_command(self) -> str:
        return "claude"

    def send(self, prompt: str, system_prompt: str = "") -> LLMResponse:
        """Send a prompt via claude -p and capture the response.

        Passes prompt via stdin to avoid command-line length limits.
        """
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        cmd = ["claude", "-p", "--output-format", "json"]

        try:
            result = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min max per call
            )

            if result.returncode != 0:
                return LLMResponse(
                    content="",
                    provider=self.name,
                    success=False,
                    error=f"claude -p failed (exit {result.returncode}): {result.stderr.strip()}",
                )

            # Parse JSON output to extract the actual text
            content = self._parse_output(result.stdout)

            return LLMResponse(
                content=content,
                provider=self.name,
                success=True,
            )

        except subprocess.TimeoutExpired:
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error="Claude CLI timed out after 5 minutes",
            )
        except FileNotFoundError:
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error="Claude CLI not found. Is claude installed and authenticated?",
            )

    def _parse_output(self, raw: str) -> str:
        """Extract text content from claude's JSON output."""
        if not raw:
            return ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for key in ("result", "content", "text", "response"):
                    if key in data:
                        return str(data[key])
                if "messages" in data:
                    messages = data["messages"]
                    if messages:
                        last = messages[-1]
                        if isinstance(last, dict) and "content" in last:
                            return str(last["content"])
            return raw.strip()
        except (json.JSONDecodeError, KeyError, IndexError):
            return raw.strip()
