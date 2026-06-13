"""BIDS dataset utilities — scan a dataset folder and identify subjects + modalities."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set


@dataclass
class Subject:
    id: str
    path: Path
    modalities: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.modalities:
            self.modalities = self._detect_modalities()

    def _detect_modalities(self) -> Set[str]:
        found: Set[str] = set()
        for mod in ("anat", "func", "dwi", "fmap"):
            d = self.path / mod
            if d.is_dir() and any(d.iterdir()):
                found.add(mod)
        return found


def scan_bids_dataset(data_dir: Path) -> List[Subject]:
    """
    Walk *data_dir* and return a sorted list of BIDS subjects.
    Returns an empty list (no crash) if the directory doesn't exist yet.
    """
    if not data_dir.exists():
        return []
    subjects = [
        Subject(id=p.name, path=p)
        for p in sorted(data_dir.glob("sub-*"))
        if p.is_dir()
    ]
    return subjects
