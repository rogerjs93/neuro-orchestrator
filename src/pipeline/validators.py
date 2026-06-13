"""
Artifact validators — standards checks beyond "a file exists".

Each validator inspects one canonical artifact (by role) and returns a pass/fail
plus QC metrics. Results are written to the manifest (valid + qc) and feed the
review gate's on_flag trigger.

Design rule: validators ADD signal, they never crash a run. Any unexpected error
(missing optional dep, unreadable variant format) degrades to ok=True with a
"skipped" note — only a clear, confident problem (empty mask, non-4D BOLD,
mostly-NaN matrix) returns ok=False.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

# Above this many disconnected components a mask is treated as likely noise.
MASK_MAX_COMPONENTS = 50
# Above this NaN fraction an FC matrix is treated as unusable.
FC_MAX_NAN_FRACTION = 0.5


@dataclass
class ValidationResult:
    ok: bool
    qc: Dict[str, Any] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)


def _skipped(reason: str) -> ValidationResult:
    return ValidationResult(True, {}, [f"validator skipped: {reason}"])


def _v_seg(path: Path) -> ValidationResult:
    try:
        import numpy as np
        import nibabel as nib
        data = np.asarray(nib.load(str(path)).get_fdata())
        labels = [int(v) for v in np.unique(data) if int(v) > 0]
        vox = int((data > 0).sum())
        ok = vox > 0 and len(labels) >= 1
        return ValidationResult(ok, {"n_labels": len(labels), "n_voxels": vox},
                                [] if ok else ["segmentation is empty or unlabeled"])
    except Exception as exc:
        return _skipped(str(exc))


def _v_preproc_bold(path: Path) -> ValidationResult:
    try:
        import nibabel as nib
        shape = tuple(int(s) for s in nib.load(str(path)).shape)  # header only, no data load
        ok = len(shape) == 4 and shape[3] > 1
        return ValidationResult(ok, {"shape": list(shape),
                                     "n_timepoints": shape[3] if len(shape) == 4 else 0},
                                [] if ok else ["preprocessed BOLD is not 4D / too few timepoints"])
    except Exception as exc:
        return _skipped(str(exc))


def _v_confounds(path: Path) -> ValidationResult:
    try:
        import csv
        with open(path, newline="") as handle:
            rows = list(csv.reader(handle, delimiter="\t"))
        ncol = len(rows[0]) if rows else 0
        nrow = max(0, len(rows) - 1)
        ok = nrow > 0 and ncol > 0
        return ValidationResult(ok, {"n_columns": ncol, "n_rows": nrow},
                                [] if ok else ["confounds table is empty"])
    except Exception as exc:
        return _skipped(str(exc))


def _v_fc_matrix(path: Path) -> ValidationResult:
    try:
        import numpy as np
        m = np.load(path)
        square = m.ndim == 2 and m.shape[0] == m.shape[1] and m.size > 0
        nan_frac = float(np.isnan(m).mean()) if m.size else 1.0
        ok = square and nan_frac <= FC_MAX_NAN_FRACTION
        msgs: List[str] = []
        if not square:
            msgs.append("FC matrix is not square / empty")
        if nan_frac > FC_MAX_NAN_FRACTION:
            msgs.append(f"FC matrix is {nan_frac:.0%} NaN")
        return ValidationResult(ok, {"shape": list(m.shape), "nan_fraction": round(nan_frac, 3)}, msgs)
    except Exception as exc:
        return _skipped(str(exc))


def _v_mask(path: Path) -> ValidationResult:
    try:
        import numpy as np
        import nibabel as nib
        from pipeline.topology import mask_topology
        data = np.asarray(nib.load(str(path)).get_fdata()) > 0
        topo = mask_topology(data)
        vox = int(topo.get("voxel_count", int(data.sum())))
        n_comp = int(topo.get("n_components", 0))
        cavities = topo.get("n_cavities")
        genus = topo.get("genus")

        msgs: List[str] = []
        ok = vox > 0
        if not ok:
            msgs.append("mask is empty")
        # Disconnected debris is a confident failure; topology quirks (handles,
        # cavities) are reported but not failed — they can be real anatomy.
        if vox > 0 and n_comp > MASK_MAX_COMPONENTS:
            ok = False
            msgs.append(f"{n_comp} disconnected components (possible noise)")
        if genus:
            msgs.append(f"genus {genus} ({genus} handle{'s' if genus != 1 else ''})")
        if cavities:
            msgs.append(f"{cavities} enclosed cavit{'ies' if cavities != 1 else 'y'}")

        qc = {
            "voxel_count": vox,
            "n_components": n_comp,
            "n_cavities": cavities,
            "genus": genus,
            "euler_number": topo.get("euler_number"),
        }
        return ValidationResult(ok, qc, msgs)
    except Exception as exc:
        return _skipped(str(exc))


def _v_json(path: Path) -> ValidationResult:
    try:
        import json
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
        ok = bool(obj)
        n = len(obj) if hasattr(obj, "__len__") else 0
        return ValidationResult(ok, {"n_entries": n}, [] if ok else ["empty JSON document"])
    except Exception as exc:
        return _skipped(str(exc))


def _v_nonempty(path: Path) -> ValidationResult:
    try:
        size = Path(path).stat().st_size
        ok = size > 0
        return ValidationResult(ok, {"bytes": int(size)}, [] if ok else ["file is empty"])
    except Exception as exc:
        return _skipped(str(exc))


_VALIDATORS: Dict[str, Callable[[Path], ValidationResult]] = {
    "seg": _v_seg,
    "preproc_bold": _v_preproc_bold,
    "confounds": _v_confounds,
    "fc_matrix": _v_fc_matrix,
    "network_metrics": _v_json,
    "iqm": _v_json,
    "mask_version": _v_mask,
}


def validate_artifact(role: str, path: Path) -> ValidationResult:
    path = Path(path)
    if not path.exists():
        return ValidationResult(False, {}, ["artifact file missing"])
    return _VALIDATORS.get(role, _v_nonempty)(path)


def validate_and_record(manifest, subject: str, roles: List[str]) -> Dict[str, ValidationResult]:
    """Validate each role's resolved artifact and write the result into the manifest."""
    out: Dict[str, ValidationResult] = {}
    for role in roles:
        path = manifest.resolve_path(subject, role)
        if path is None:
            continue
        res = validate_artifact(role, path)
        manifest.set_validation(subject, role, valid=res.ok, qc=res.qc)
        out[role] = res
    return out
