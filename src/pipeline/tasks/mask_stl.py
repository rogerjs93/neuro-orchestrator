"""
3D mask + STL export stage.

Produces the analytical ROI mask (always) and, optionally, a printable STL mesh.
This is the orchestrated *auto* path: it builds an automatic baseline mask from
the segmentation and writes it as a versioned artifact in the same layout the web
UI manual editor reads, so a gated review can pick up exactly where this left off.

The mask is always written (researchers want the ROI volume); STL export is
controlled by --export-stl/--no-stl (makers / 3D-print users).

Called as:
  python -m pipeline.tasks.mask_stl --subject sub-001 \
      --fastsurfer-dir /outputs/fastsurfer --output-dir /outputs \
      --selection by_tissue --groups gray_matter --stl-preset standard
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import nibabel as nib
import numpy as np

from pipeline.tasks.stl_export import (
    PRESETS,
    _build_mask,
    _find_segmentation,
    _postprocess_mask,
    generate_stl,
    get_mask_catalog,
)

# Selection modes accepted by this stage. `whole` and `external_cortex` need no
# explicit labels; the `by_*` modes require labels or groups to resolve against.
SELECTIONS = {"whole", "external_cortex", "by_region", "by_lobe", "by_network", "by_tissue"}
_GROUPED = {"by_lobe", "by_network", "by_tissue"}
_LABELLED = {"by_region", "by_lobe", "by_network", "by_tissue"}


def _resolve_labels(
    subject_id: str,
    fastsurfer_dir: Path,
    selection: str,
    labels: Optional[List[Any]],
    groups: Optional[List[str]],
) -> List[int]:
    """Turn an explicit label list, or group names for the by_* modes, into label ids."""
    explicit: List[int] = []
    for value in (labels or []):
        try:
            explicit.append(int(value))
        except (TypeError, ValueError):
            continue
    if explicit:
        return sorted(set(explicit))

    if selection in _GROUPED and groups:
        catalog = get_mask_catalog(subject_id, fastsurfer_dir)
        group_map = catalog.get("groups", {}).get(selection, {})
        resolved: List[int] = []
        for group_name in groups:
            resolved.extend(int(x) for x in group_map.get(group_name, []))
        return sorted(set(resolved))

    return []


def _mask_params(selection: str, resolved_labels: List[int]) -> Dict[str, Any]:
    """Build the params dict that _build_mask / _postprocess_mask consume."""
    params: Dict[str, Any] = dict(PRESETS["standard"])
    if selection == "external_cortex":
        params["external_cortex_only"] = True
    if resolved_labels:
        params["include_labels"] = resolved_labels
    return params


def _save_mask_version(
    *,
    subject_id: str,
    output_dir: Path,
    mask: np.ndarray,
    reference_img: nib.spatialimages.SpatialImage,
    selection: str,
    resolved_labels: List[int],
    source_segmentation: Path,
    parent_version_id: Optional[str],
) -> Dict[str, Any]:
    """Write an editor-compatible mask version (NIfTI + JSON sidecar).

    Mirrors the layout/metadata that web_server._save_mask_version produces, so the
    manual editor lists and loads this auto baseline like any other version.
    """
    versions_dir = output_dir / "masks" / subject_id / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)

    version_id = datetime.now(timezone.utc).strftime("v%Y%m%dT%H%M%S%fZ")
    nifti_path = versions_dir / f"{version_id}.nii.gz"
    sidecar_path = versions_dir / f"{version_id}.json"

    mask_u8 = (mask > 0).astype(np.uint8)
    out_img = nib.Nifti1Image(mask_u8, reference_img.affine, reference_img.header)
    nib.save(out_img, str(nifti_path))

    meta: Dict[str, Any] = {
        "version_id": version_id,
        "subject_id": subject_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parent_version_id": parent_version_id,
        "source_type": "auto",
        "operation_summary": f"pipeline_auto_mask:{selection}",
        "voxel_count": int(np.count_nonzero(mask_u8)),
        "shape": [int(v) for v in mask_u8.shape],
        "selection": selection,
        "selected_labels": resolved_labels,
        "source_segmentation": str(source_segmentation),
    }
    sidecar_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    meta["relative_nifti"] = str(nifti_path.relative_to(output_dir))
    return meta


def run(
    *,
    subject_id: str,
    fastsurfer_dir: Path,
    output_dir: Path,
    selection: str = "whole",
    labels: Optional[List[Any]] = None,
    groups: Optional[List[str]] = None,
    export_stl: bool = True,
    stl_preset: str = "standard",
    parent_version_id: Optional[str] = None,
) -> None:
    selection = (selection or "whole").strip().lower()
    if selection not in SELECTIONS:
        raise ValueError(f"Unknown selection '{selection}'. One of: {sorted(SELECTIONS)}")
    if stl_preset not in PRESETS:
        raise ValueError(f"Unknown STL preset '{stl_preset}'. One of: {sorted(PRESETS)}")

    print(f"[mask] {subject_id}: selection={selection} export_stl={export_stl}")

    # Raises FileNotFoundError with a clear message if segmentation is missing —
    # the stage depends on a completed segmentation step.
    seg_file = _find_segmentation(subject_id, fastsurfer_dir)
    print(f"[mask] segmentation: {seg_file.name}")

    resolved = _resolve_labels(subject_id, fastsurfer_dir, selection, labels, groups)
    if selection in _LABELLED and not resolved:
        raise RuntimeError(
            f"Selection '{selection}' requires explicit labels or groups; none resolved. "
            "Pass --labels or --groups, or use selection=whole / external_cortex."
        )

    img = nib.load(str(seg_file))
    data = np.asarray(img.get_fdata())
    params = _mask_params(selection, resolved)

    print("[mask] building baseline mask")
    mask = _build_mask(data, params)
    mask = _postprocess_mask(mask, params)
    voxels = int(np.count_nonzero(mask))
    if voxels == 0:
        raise RuntimeError(
            "Baseline mask is empty after filtering — relax the selection or label set."
        )
    print(f"[mask] baseline voxels: {voxels}")

    version = _save_mask_version(
        subject_id=subject_id,
        output_dir=output_dir,
        mask=mask,
        reference_img=img,
        selection=selection,
        resolved_labels=resolved,
        source_segmentation=seg_file,
        parent_version_id=parent_version_id,
    )
    print(f"[mask] saved baseline version {version['version_id']} -> {version['relative_nifti']}")

    if export_stl:
        print(f"[mask] exporting STL (preset={stl_preset})")
        overrides: Dict[str, Any] = {}
        if resolved:
            overrides["include_labels"] = resolved
        if selection == "external_cortex":
            overrides["external_cortex_only"] = True
        result = generate_stl(
            subject_id=subject_id,
            fastsurfer_dir=fastsurfer_dir,
            output_dir=output_dir / "stl",
            preset=stl_preset,
            overrides=overrides,
        )
        print(f"[mask] STL: {result.stl_path} ({result.vertices} verts / {result.faces} faces)")
    else:
        print("[mask] STL export skipped (export_stl=false)")

    print("[mask] done")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--fastsurfer-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Top-level outputs dir; masks/ and stl/ are written under it.")
    parser.add_argument("--selection", default="whole", choices=sorted(SELECTIONS))
    parser.add_argument("--labels", default="", help="Comma-separated label ids for by_region.")
    parser.add_argument("--groups", default="", help="Comma-separated group names for by_lobe/network/tissue.")
    parser.add_argument("--stl-preset", default="standard", choices=sorted(PRESETS))
    stl_group = parser.add_mutually_exclusive_group()
    stl_group.add_argument("--export-stl", dest="export_stl", action="store_true")
    stl_group.add_argument("--no-stl", dest="export_stl", action="store_false")
    parser.set_defaults(export_stl=True)
    args = parser.parse_args()

    labels = [s for s in (args.labels.split(",") if args.labels else []) if s.strip()]
    groups = [s for s in (args.groups.split(",") if args.groups else []) if s.strip()]

    run(
        subject_id=args.subject,
        fastsurfer_dir=args.fastsurfer_dir,
        output_dir=args.output_dir,
        selection=args.selection,
        labels=labels,
        groups=groups,
        export_stl=args.export_stl,
        stl_preset=args.stl_preset,
    )


if __name__ == "__main__":
    main()
