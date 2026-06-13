"""Checkpoint persistence helpers for restart-safe pipeline resume."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1
STATE_DIR_NAME = "state"
STATE_FILE_NAME = "pipeline_state.json"


def checkpoint_path(output_dir: Path) -> Path:
    return output_dir / STATE_DIR_NAME / STATE_FILE_NAME


def load_checkpoint(output_dir: Path) -> Optional[Dict[str, Any]]:
    path = checkpoint_path(output_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_checkpoint(output_dir: Path, payload: Dict[str, Any]) -> Path:
    path = checkpoint_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: temp file in target directory, then replace.
    fd, temp_name = tempfile.mkstemp(prefix="pipeline_state_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    return path
