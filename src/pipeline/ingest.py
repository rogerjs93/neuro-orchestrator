"""
DICOM -> BIDS ingestion via dcm2bids (the clinician front door).

dcm2bids (config-driven, wraps dcm2niix) is run as a sibling Docker container —
same pattern as the pipeline tool stages, so nothing needs installing on the host.
This module only builds the command + a starter config; the orchestrator runs it
and the existing BIDS scan then picks up the new subject under data/.

Reference: Boré et al. (2023), dcm2bids, JOSS 8(85):5750.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_IMAGE = "unfmontreal/dcm2bids:latest"

# Starter config (dcm2bids v3 schema). Criteria are site-specific glob matches on
# DICOM fields — users tune these to their scanner's SeriesDescription values.
DEFAULT_CONFIG: Dict[str, Any] = {
    "descriptions": [
        {"datatype": "anat", "suffix": "T1w", "criteria": {"SeriesDescription": "*T1*"}},
        {"datatype": "anat", "suffix": "T2w", "criteria": {"SeriesDescription": "*T2*"}},
        {"datatype": "func", "suffix": "bold", "custom_entities": "task-rest",
         "criteria": {"SeriesDescription": "*rest*"}},
        {"datatype": "dwi", "suffix": "dwi", "criteria": {"SeriesDescription": "*DWI*"}},
    ]
}


def participant_label(participant: str) -> str:
    """Normalise to a bare BIDS label (dcm2bids -p adds the sub- prefix itself)."""
    p = participant.strip()
    return p[4:] if p.lower().startswith("sub-") else p


def write_default_config(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return path


def build_dcm2bids_command(
    *,
    dicom_dir: str,
    participant: str,
    output_dir: str,
    config: str,
    session: Optional[str] = None,
    image: str = DEFAULT_IMAGE,
    docker_prefix: Sequence[str] = ("docker", "run", "--rm"),
) -> List[str]:
    """Build the docker command that runs dcm2bids for one subject.

    Mounts the DICOM dir read-only, the BIDS output (data/) read-write, and the
    config read-only; emits sub-<label> under the output dir.
    """
    label = participant_label(participant)
    cmd: List[str] = list(docker_prefix) + [
        "-v", f"{dicom_dir}:/dicom:ro",
        "-v", f"{output_dir}:/bids",
        "-v", f"{config}:/config.json:ro",
        image,
        "-d", "/dicom",
        "-p", label,
        "-c", "/config.json",
        "-o", "/bids",
    ]
    if session:
        cmd += ["-s", str(session)]
    return cmd
