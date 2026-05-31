"""Codex CLI provider — uses codex exec with Plus subscription."""

import json
import subprocess
import shutil
import tempfile
import os
import sys
from pathlib import Path
from .base import LLMProvider, LLMResponse

# Transient Windows error: Codex's sandbox spawn occasionally fails on first
# attempt and succeeds on retry (observed live, codex-cli 0.135.0).
_SPAWN_REFRESH = "windows sandbox: spawn setup refresh"
_MAX_ATTEMPTS = 3


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

    def _send(self, prompt: str, system_prompt: str = "", cwd: str = "") -> LLMResponse:
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

        codex_bin = self._cli_command()

        # Determine working directory — codex requires a git repo or --skip-git-repo-check
        work_dir = cwd or os.environ.get("CLAUDEX_PROJECT_DIR", "")

        def _build_cmd(out_path: str) -> list:
            c = [codex_bin, "exec", "--json", "--ephemeral", "-o", out_path]
            if work_dir:
                c.extend(["-C", work_dir])
            else:
                c.append("--skip-git-repo-check")
            c.append("-")  # read prompt from stdin
            return c

        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        output_path = ""
        try:
            content = ""
            returncode = 0
            stderr = ""
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                # Fresh -o file per attempt — never reuse an unlinked path on retry.
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8"
                ) as out_tmp:
                    output_path = out_tmp.name

                # Pipe the prompt file as stdin for clean UTF-8
                with open(prompt_path, "r", encoding="utf-8") as stdin_file:
                    result = subprocess.run(
                        _build_cmd(output_path),
                        stdin=stdin_file,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=900,  # 15 min for deep-audit workloads
                    )
                returncode = result.returncode
                stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
                stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")

                # Final message: prefer the -o file, fall back to the JSONL agent_message.
                file_content = ""
                if os.path.exists(output_path):
                    file_content = Path(output_path).read_text(encoding="utf-8").strip()
                    os.unlink(output_path)

                last_msg, parsed_usage = self._parse_jsonl(stdout)
                if any(parsed_usage.values()):
                    usage = parsed_usage
                content = file_content or last_msg

                spawn_refresh = _SPAWN_REFRESH in stdout or _SPAWN_REFRESH in stderr
                if not content and spawn_refresh and attempt < _MAX_ATTEMPTS:
                    continue  # transient Windows sandbox spawn error — retry
                break

            # Clean up prompt file
            if os.path.exists(prompt_path):
                os.unlink(prompt_path)

            ok = returncode == 0 and bool(content)
            if ok:
                error = ""
            elif not content:
                error = f"codex exec failed (exit {returncode}): {stderr.strip()}"
            else:
                error = f"Codex exit={returncode}, output=yes"

            return LLMResponse(
                content=content,
                provider=self.name,
                success=ok,
                error=error,
                **usage,
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

    @staticmethod
    def _parse_jsonl(stdout: str) -> tuple[str, dict]:
        """Parse Codex `--json` JSONL stdout.

        Returns (last_agent_message, usage). usage keys: input_tokens,
        cached_input_tokens, output_tokens (zeros if not reported). The final
        agent message is a fallback for content when the -o file is empty.
        """
        last_msg = ""
        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "turn.completed":
                u = ev.get("usage") or {}
                if isinstance(u, dict):
                    usage = {
                        "input_tokens": int(u.get("input_tokens", 0) or 0),
                        "cached_input_tokens": int(u.get("cached_input_tokens", 0) or 0),
                        "output_tokens": int(u.get("output_tokens", 0) or 0),
                    }
            elif ev.get("type") == "item.completed":
                item = ev.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                    last_msg = str(item["text"]).strip()
        return last_msg, usage
