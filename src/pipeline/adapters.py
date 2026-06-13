"""
Tool output adapters — register canonical artifacts after a stage completes.

Each tool writes its own native layout; these adapters encode the tool-specific
discovery in ONE place and register the canonical ROLE(s) into the manifest, with
provenance edges to upstream roles. Downstream code then resolves by role instead
of globbing tool filenames (that swap is the next step, A2).

Best-effort and non-raising: a missing output is skipped, never fatal — this
layer only *adds* to the ledger and must not break a working run. `--output-dir`
here is the top-level outputs root (masks/ and stl/ live directly under it).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

from pipeline.manifest import ArtifactManifest

# Default producing tool per stage (until per-run tool selection is wired).
# Provenance is best-effort; the swap to configured tools comes with the builder.
DEFAULT_TOOLS = {
    "mriqc": "MRIQC",
    "fastsurfer": "FastSurfer",
    "fmriprep": "fMRIPrep",
    "mrtrix3": "MRtrix3",
    "connectivity": "Nilearn",
    "network": "BCT + NetworkX",
    "mask": "built-in mesher",
}


def _first(base: Path, *patterns: str) -> Optional[Path]:
    for pat in patterns:
        hits = sorted(base.glob(pat))
        if hits:
            return hits[0]
    return None


def _latest(paths: Iterable[Path]) -> Optional[Path]:
    # Timestamped names (version ids, run stamps) sort chronologically.
    files = sorted(p for p in paths if p.is_file())
    return files[-1] if files else None


def register_stage_outputs(
    manifest: ArtifactManifest,
    *,
    subject: str,
    stage: str,
    output_dir: Path,
    tool: str = "",
    tool_version: str = "",
) -> List[str]:
    """Locate and register a stage's canonical artifacts. Returns the roles registered."""
    output_dir = Path(output_dir)
    tool = tool or DEFAULT_TOOLS.get(stage, "")
    registered: List[str] = []

    def reg(role: str, path: Optional[Path], inputs: Optional[List[str]] = None) -> None:
        if path is None or not Path(path).is_file():
            return
        manifest.register(
            subject=subject, role=role, path=Path(path), stage=stage,
            tool=tool, tool_version=tool_version,
            inputs=manifest.input_refs(subject, inputs or []),
        )
        registered.append(role)

    if stage == "mriqc":
        out = output_dir / "mriqc"
        reg("iqm", _first(out, f"{subject}/**/*{subject}*.json", f"**/*{subject}*.json",
                          f"**/*{subject}*.html"))

    elif stage == "fastsurfer":
        out = output_dir / "fastsurfer"
        reg("seg", _first(out,
                          f"{subject}/**/aparc.DKTatlas+aseg.deep.mgz",
                          f"{subject}/**/aparc+aseg.mgz",
                          f"{subject}/**/aseg.mgz",
                          f"{subject}/**/*aseg*.mgz",
                          f"{subject}/**/*aseg*.nii.gz"))

    elif stage == "fmriprep":
        out = output_dir / "fmriprep"
        reg("preproc_bold", _first(out,
                                   f"{subject}/**/*space-MNI*preproc_bold.nii.gz",
                                   f"{subject}/**/*desc-preproc*_bold.nii.gz"))
        reg("confounds", _first(out,
                                f"{subject}/**/*confounds_timeseries.tsv",
                                f"{subject}/**/*confounds*.tsv"))

    elif stage == "mrtrix3":
        out = output_dir / "mrtrix3"
        reg("tracks", _first(out, f"{subject}/tracks.tck", f"{subject}/**/*.tck"))

    elif stage == "connectivity":
        out = output_dir / "connectivity"
        reg("fc_matrix", _first(out, f"{subject}_fc_matrix.npy"),
            inputs=["preproc_bold", "confounds"])
        reg("atlas_labels", _first(out, f"{subject}_atlas_labels.json"))

    elif stage == "network":
        out = output_dir / "network"
        reg("network_metrics", _first(out, f"{subject}_network_metrics.json"),
            inputs=["fc_matrix"])

    elif stage == "mask":
        versions = output_dir / "masks" / subject / "versions"
        reg("mask_version", _latest(versions.glob("*.nii.gz")) if versions.is_dir() else None,
            inputs=["seg"])
        stl_dir = output_dir / "stl" / subject
        reg("stl", _latest(stl_dir.glob("*.stl")) if stl_dir.is_dir() else None,
            inputs=["mask_version"])

    return registered
