"""Claude Code CLI provider — uses claude -p (pipe mode) with Pro subscription.

Windows-aware transport:
- Writes the prompt to a UTF-8 temp file and passes the file handle as stdin
  (mirrors `codex.py`). This avoids two Windows-only failure modes that the
  earlier `subprocess.run(input=..., text=True)` path was vulnerable to:
    1. cp1252 encoding crash when the prompt contains characters outside
       latin-1 (e.g. emoji severity markers in audit docs:
       UnicodeEncodeError: 'charmap' codec can't encode character '\\U0001f534').
    2. claude -p's 3-second stdin readiness timer racing Python's internal
       writer thread on large prompts ("no stdin data received in 3s").
  Both were observed live during the 2026-05-13 Codex full-audit run.
- Resolves the executable via `shutil.which("claude") or
  shutil.which("claude.cmd")` to handle Windows .cmd shims.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .base import LLMProvider, LLMResponse


class ClaudeProvider(LLMProvider):
    """Wraps the Claude Code CLI in non-interactive pipe mode."""

    @property
    def name(self) -> str:
        return "claude"

    def _cli_command(self) -> str:
        # On Windows, the launcher is a .cmd shim — subprocess needs the full path.
        if sys.platform == "win32":
            cmd_path = shutil.which("claude") or shutil.which("claude.cmd")
            return cmd_path or "claude"
        return "claude"

    def send(self, prompt: str, system_prompt: str = "") -> LLMResponse:
        """Send a prompt via claude -p and capture the response.

        Writes the prompt to a temp file and pipes it via stdin to avoid both
        Windows command-line length limits and the cp1252 encoding crash
        triggered when the prompt contains non-latin-1 characters (emoji,
        long-dash, etc.). See module docstring for the failure modes this
        guards against.
        """
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        # Write prompt to temp file to ensure clean UTF-8 transport.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as prompt_tmp:
            prompt_tmp.write(full_prompt)
            prompt_path = prompt_tmp.name

        claude_bin = self._cli_command()
        cmd = [claude_bin, "-p", "--output-format", "json"]

        try:
            with open(prompt_path, "r", encoding="utf-8") as stdin_file:
                result = subprocess.run(
                    cmd,
                    stdin=stdin_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=900,  # 15 min — bumped from 300s for deep-audit workloads
                )

            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")

            if result.returncode != 0:
                return LLMResponse(
                    content="",
                    provider=self.name,
                    success=False,
                    error=f"claude -p failed (exit {result.returncode}): {stderr.strip()}",
                )

            return LLMResponse(
                content=self._parse_output(stdout),
                provider=self.name,
                success=True,
            )

        except subprocess.TimeoutExpired:
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error="Claude CLI timed out after 15 minutes",
            )
        except FileNotFoundError:
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error="Claude CLI not found. Is claude installed and authenticated?",
            )
        finally:
            if os.path.exists(prompt_path):
                try:
                    os.unlink(prompt_path)
                except OSError:
                    pass

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
