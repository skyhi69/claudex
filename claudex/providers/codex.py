"""Codex CLI provider — uses codex exec with Plus subscription."""

import subprocess
import shutil
import tempfile
import os
import sys
from pathlib import Path
from .base import LLMProvider, LLMResponse


class CodexProvider(LLMProvider):
    """Wraps the OpenAI Codex CLI in non-interactive exec mode."""

    @property
    def name(self) -> str:
        return "codex"

    def _cli_command(self) -> str:
        # On Windows, npm installs .cmd shims — subprocess needs full path
        if sys.platform == "win32":
            cmd_path = shutil.which("codex") or shutil.which("codex.cmd")
            return cmd_path or "codex"
        return "codex"

    def send(self, prompt: str, system_prompt: str = "", cwd: str = "") -> LLMResponse:
        """Send a prompt via codex exec and capture the response.

        Writes prompt to a temp file and pipes it via stdin to avoid both
        Windows command-line length limits and encoding issues.
        Uses -o flag for reliable output capture.

        Args:
            cwd: Working directory for codex. If empty, uses the claudex project dir.
        """
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        # Write prompt to temp file to ensure clean UTF-8
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as prompt_tmp:
            prompt_tmp.write(full_prompt)
            prompt_path = prompt_tmp.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as out_tmp:
            output_path = out_tmp.name

        codex_bin = self._cli_command()

        # Determine working directory — codex requires a git repo or --skip-git-repo-check
        work_dir = cwd or os.environ.get("CLAUDEX_PROJECT_DIR", "")

        cmd = [
            codex_bin, "exec",
            "--ephemeral",
            "-o", output_path,
        ]
        if work_dir:
            cmd.extend(["-C", work_dir])
        else:
            cmd.append("--skip-git-repo-check")
        cmd.append("-")  # read prompt from stdin

        try:
            # Pipe the prompt file as stdin for clean UTF-8
            with open(prompt_path, "r", encoding="utf-8") as stdin_file:
                result = subprocess.run(
                    cmd,
                    stdin=stdin_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=900,  # 15 min — bumped from 300s for deep-audit workloads
                )

            # Clean up prompt file
            if os.path.exists(prompt_path):
                os.unlink(prompt_path)

            # Read output from the temp file
            content = ""
            if os.path.exists(output_path):
                content = Path(output_path).read_text(encoding="utf-8").strip()
                os.unlink(output_path)

            if result.returncode != 0 and not content:
                stderr = result.stderr.decode("utf-8", errors="replace").strip() if isinstance(result.stderr, bytes) else str(result.stderr).strip()
                return LLMResponse(
                    content="",
                    provider=self.name,
                    success=False,
                    error=f"codex exec failed (exit {result.returncode}): {stderr}",
                )

            # If we got content from the output file, use it
            # Otherwise fall back to stdout
            if not content:
                content = result.stdout.strip()

            return LLMResponse(
                content=content,
                provider=self.name,
                success=(result.returncode == 0 and bool(content)),
                error="" if (result.returncode == 0 and content) else f"Codex exit={result.returncode}, output={'yes' if content else 'empty'}",
            )

        except subprocess.TimeoutExpired:
            for p in (prompt_path, output_path):
                if os.path.exists(p):
                    os.unlink(p)
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error="Codex CLI timed out after 15 minutes",
            )
        except (FileNotFoundError, OSError) as e:
            for p in (prompt_path, output_path):
                if os.path.exists(p):
                    os.unlink(p)
            return LLMResponse(
                content="",
                provider=self.name,
                success=False,
                error=f"Codex CLI error: {e}",
            )
