"""
Artifact manifest — the data-management ledger.

Decouples downstream stages from tool-specific filenames. Every stage registers
the canonical artifact(s) it produced (by ROLE, not filename); downstream code
resolves by role. Each record carries provenance (tool + digest + params), a
content hash, the input hashes it was built from, and QC/validation status.

Canonical outputs live under a BIDS-Derivatives root (outputs/derivatives), so
results are reproducible and interoperable with other BIDS tools. The manifest
itself is a JSON index over that tree (.neuro_manifest.json).

This module is pure data management — no web/FastAPI/pipeline coupling — so the
runner, the web server, and standalone tasks can all share it.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MANIFEST_FILENAME = ".neuro_manifest.json"
MANIFEST_SCHEMA_VERSION = 1
BIDS_VERSION = "1.9.0"

# Canonical artifact roles. Downstream stages ask for these, never for a
# tool-specific filename. Each producing stage may emit one or more roles.
ROLES = {
    "iqm",            # mriqc image-quality metrics
    "seg",            # brain segmentation label volume (FastSurfer/FreeSurfer/SynthSeg/ANTs)
    "preproc_bold",   # preprocessed BOLD (fMRIPrep/C-PAC/FEAT)
    "confounds",      # nuisance regressors
    "tracks",         # tractography streamlines (MRtrix3/DIPY/DSI Studio)
    "fc_matrix",      # functional connectivity matrix (Nilearn)
    "atlas_labels",   # parcellation labels for the FC matrix
    "network_metrics",# graph metrics (BCT/NetworkX)
    "mask_version",   # a versioned 3D mask
    "stl",            # exported STL mesh
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, _chunk: int = 1 << 20) -> str:
    """Content hash of a file, streamed so large NIfTI volumes don't blow memory."""
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class Artifact:
    role: str
    subject: str
    path: str                     # relative to the derivatives root, POSIX style
    stage: str
    tool: str = ""
    tool_version: str = ""        # version or image digest — provenance
    params: Dict[str, Any] = field(default_factory=dict)
    sha256: str = ""
    inputs: List[Dict[str, str]] = field(default_factory=list)  # [{role, sha256}]
    qc: Dict[str, Any] = field(default_factory=dict)
    valid: Optional[bool] = None  # None = not yet validated
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Artifact":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class ArtifactManifest:
    """JSON-backed ledger of canonical artifacts, keyed by (subject, role, path)."""

    def __init__(self, derivatives_dir: Path) -> None:
        self.root = Path(derivatives_dir)
        self.path = self.root / MANIFEST_FILENAME
        self._artifacts: List[Artifact] = []
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> None:
        self._artifacts = []
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in data.get("artifacts", []):
            if isinstance(item, dict) and item.get("role") and item.get("subject"):
                self._artifacts.append(Artifact.from_dict(item))

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "saved_at": _utc_now(),
            "artifacts": [a.to_dict() for a in self._artifacts],
        }
        fd, tmp = tempfile.mkstemp(prefix="manifest_", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ── write ────────────────────────────────────────────────────────────────
    def register(
        self,
        *,
        subject: str,
        role: str,
        path: Path,
        stage: str,
        tool: str = "",
        tool_version: str = "",
        params: Optional[Dict[str, Any]] = None,
        inputs: Optional[List[Dict[str, str]]] = None,
        qc: Optional[Dict[str, Any]] = None,
        valid: Optional[bool] = None,
    ) -> Artifact:
        """Register a produced artifact. Path may be absolute or relative to the root.

        Replaces any prior record with the same (subject, role, rel_path) so a
        re-run updates in place rather than duplicating.
        """
        if role not in ROLES:
            raise ValueError(f"Unknown artifact role '{role}'. Known: {sorted(ROLES)}")
        abs_path = path if path.is_absolute() else (self.root / path)
        try:
            rel = abs_path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            # Outside the derivatives root — store the path as given (best effort).
            rel = abs_path.as_posix()

        digest = sha256_file(abs_path) if abs_path.is_file() else ""
        artifact = Artifact(
            role=role, subject=subject, path=rel, stage=stage, tool=tool,
            tool_version=tool_version, params=dict(params or {}),
            sha256=digest, inputs=list(inputs or []), qc=dict(qc or {}), valid=valid,
        )
        self._artifacts = [
            a for a in self._artifacts
            if not (a.subject == subject and a.role == role and a.path == rel)
        ]
        self._artifacts.append(artifact)
        self._save()
        return artifact

    def set_validation(self, subject: str, role: str, *, valid: bool,
                       qc: Optional[Dict[str, Any]] = None) -> Optional[Artifact]:
        art = self.resolve(subject, role)
        if art is None:
            return None
        art.valid = valid
        if qc:
            art.qc.update(qc)
        self._save()
        return art

    # ── read ─────────────────────────────────────────────────────────────────
    def list(self, subject: Optional[str] = None, role: Optional[str] = None) -> List[Artifact]:
        out = self._artifacts
        if subject is not None:
            out = [a for a in out if a.subject == subject]
        if role is not None:
            out = [a for a in out if a.role == role]
        return sorted(out, key=lambda a: a.created_at, reverse=True)

    def resolve(self, subject: str, role: str) -> Optional[Artifact]:
        """Latest artifact for (subject, role); prefers a validated one if any exist."""
        candidates = self.list(subject=subject, role=role)
        if not candidates:
            return None
        valid = [a for a in candidates if a.valid is True]
        return valid[0] if valid else candidates[0]

    def resolve_path(self, subject: str, role: str) -> Optional[Path]:
        art = self.resolve(subject, role)
        if art is None:
            return None
        p = Path(art.path)
        return p if p.is_absolute() else (self.root / p)

    def is_stale(self, subject: str, role: str) -> bool:
        """True if any recorded input hash no longer matches the current upstream artifact."""
        art = self.resolve(subject, role)
        if art is None:
            return False
        for ref in art.inputs:
            current = self.resolve(subject, str(ref.get("role", "")))
            if current is None or not current.sha256 or not ref.get("sha256"):
                continue
            if current.sha256 != ref["sha256"]:
                return True
        return False

    def input_refs(self, subject: str, roles: List[str]) -> List[Dict[str, str]]:
        """Build provenance edges (role + hash) for the given upstream roles."""
        refs: List[Dict[str, str]] = []
        for r in roles:
            art = self.resolve(subject, r)
            if art is not None:
                refs.append({"role": r, "sha256": art.sha256})
        return refs


def ensure_dataset_description(
    derivatives_dir: Path,
    *,
    name: str = "neuro-orchestrator derivatives",
    generated_by: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Write/refresh a minimal BIDS-Derivatives dataset_description.json."""
    derivatives_dir.mkdir(parents=True, exist_ok=True)
    desc_path = derivatives_dir / "dataset_description.json"
    desc = {
        "Name": name,
        "BIDSVersion": BIDS_VERSION,
        "DatasetType": "derivative",
        "GeneratedBy": generated_by or [{"Name": "neuro-orchestrator"}],
    }
    desc_path.write_text(json.dumps(desc, indent=2) + "\n", encoding="utf-8")
    return desc_path
