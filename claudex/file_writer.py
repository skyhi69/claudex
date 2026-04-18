"""Safe file writer with backup support."""

import shutil
from pathlib import Path

from .models import FileOutput


def write_files(files: list[FileOutput], target_dir: Path, backup: bool = True) -> list[str]:
    """Write generated files to disk with optional backups.

    Args:
        files: List of FileOutput objects to write
        target_dir: Base directory for file output
        backup: If True, create .claudex.bak for overwritten files

    Returns:
        List of summary strings describing what was written
    """
    summaries = []

    for f in files:
        file_path = target_dir / f.path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if f.action == "delete":
            if file_path.exists():
                if backup:
                    shutil.copy2(file_path, str(file_path) + ".claudex.bak")
                file_path.unlink()
                summaries.append(f"  - {f.path} (deleted, backup saved)")
            else:
                summaries.append(f"  - {f.path} (already absent, skipped)")

        elif f.action in ("create", "modify"):
            existed = file_path.exists()

            if existed and backup:
                shutil.copy2(file_path, str(file_path) + ".claudex.bak")

            file_path.write_text(f.content, encoding="utf-8")

            line_count = f.content.count("\n") + 1
            action_label = "modified" if existed else "created"
            backup_note = ", backup saved" if existed and backup else ""
            summaries.append(f"  + {f.path} ({action_label}, {line_count} lines{backup_note})")

    return summaries
