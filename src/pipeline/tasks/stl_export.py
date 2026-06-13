"""
Parametric STL export from FastSurfer segmentation outputs.

Usage:
  python -m pipeline.tasks.stl_export --subject sub-001 --fastsurfer-dir /outputs/fastsurfer --output-dir /outputs/stl
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import nibabel as nib
import numpy as np
from scipy.ndimage import binary_closing, binary_opening, generate_binary_structure, label
from skimage import measure
import trimesh


PRESETS: Dict[str, Dict[str, Any]] = {
    "standard": {
        "smoothing_iterations": 8,
        "smoothing_lambda": 0.45,
        "min_component_voxels": 1000,
        "open_iterations": 0,
        "close_iterations": 1,
        "decimation_ratio": 0.65,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
    },
    "high_quality": {
        "smoothing_iterations": 14,
        "smoothing_lambda": 0.35,
        "min_component_voxels": 500,
        "open_iterations": 0,
        "close_iterations": 1,
        "decimation_ratio": 0.9,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
    },
    "fast_preview": {
        "smoothing_iterations": 4,
        "smoothing_lambda": 0.55,
        "min_component_voxels": 2500,
        "open_iterations": 1,
        "close_iterations": 1,
        "decimation_ratio": 0.2,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
    },
    "external_cortex": {
        "smoothing_iterations": 10,
        "smoothing_lambda": 0.4,
        "min_component_voxels": 1200,
        "open_iterations": 0,
        "close_iterations": 1,
        "decimation_ratio": 0.55,
        "external_cortex_only": True,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
    },
    "by_region": {
        "smoothing_iterations": 8,
        "smoothing_lambda": 0.4,
        "min_component_voxels": 400,
        "open_iterations": 0,
        "close_iterations": 1,
        "decimation_ratio": 0.6,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
        "split_by_label": True,
    },
    "by_lobe": {
        "smoothing_iterations": 8,
        "smoothing_lambda": 0.4,
        "min_component_voxels": 500,
        "open_iterations": 0,
        "close_iterations": 1,
        "decimation_ratio": 0.65,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
        "split_by_label": True,
    },
    "by_network": {
        "smoothing_iterations": 8,
        "smoothing_lambda": 0.4,
        "min_component_voxels": 450,
        "open_iterations": 0,
        "close_iterations": 1,
        "decimation_ratio": 0.65,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
        "split_by_label": True,
    },
    "by_tissue": {
        "smoothing_iterations": 8,
        "smoothing_lambda": 0.4,
        "min_component_voxels": 600,
        "open_iterations": 0,
        "close_iterations": 1,
        "decimation_ratio": 0.65,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
        "split_by_label": True,
    },
    "noise_suppressed": {
        "smoothing_iterations": 10,
        "smoothing_lambda": 0.35,
        "min_component_voxels": 5000,
        "open_iterations": 2,
        "close_iterations": 2,
        "decimation_ratio": 0.7,
        "external_cortex_only": False,
        "include_labels": [],
        "exclude_labels": [],
        "normalize_signal": False,
        "intensity_threshold": None,
    },
}


@dataclass
class STLArtifact:
    stl_path: Path
    sidecar_path: Path
    vertices: int
    faces: int
    decimation_mode: str


@dataclass
class STLResult:
    stl_path: Path
    sidecar_path: Path
    source_segmentation: Path
    vertices: int
    faces: int
    artifacts: List[STLArtifact]


def _find_segmentation(subject_id: str, fastsurfer_dir: Path) -> Path:
    candidates = [
        f"{subject_id}/**/aparc.DKTatlas+aseg.deep.mgz",
        f"{subject_id}/**/aparc+aseg.mgz",
        f"{subject_id}/**/aseg.mgz",
        f"{subject_id}/**/*aseg*.mgz",
        f"{subject_id}/**/*aseg*.nii.gz",
        f"{subject_id}/**/*aseg*.nii",
    ]
    for pattern in candidates:
        hits = sorted(fastsurfer_dir.glob(pattern))
        if hits:
            return hits[0]
    raise FileNotFoundError(
        f"No FastSurfer segmentation file found for {subject_id} under {fastsurfer_dir}"
    )


def _as_int_list(values: Iterable[Any]) -> List[int]:
    out: List[int] = []
    for v in values:
        if isinstance(v, str) and not v.strip():
            continue
        out.append(int(v))
    return out


def _slug(s: str) -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "-" for ch in (s or ""))
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw.strip("-") or "unknown"


_LABEL_NAME_OVERRIDES: Dict[str, str] = {
    "bankssts": "Banks of Superior Temporal Sulcus",
    "caudalmiddlefrontal": "Caudal Middle Frontal",
    "cuneus": "Cuneus",
    "entorhinal": "Entorhinal",
    "frontalpole": "Frontal Pole",
    "fusiform": "Fusiform",
    "hippocampus": "Hippocampus",
    "inferiorparietal": "Inferior Parietal",
    "inferiortemporal": "Inferior Temporal",
    "insula": "Insula",
    "isthmuscingulate": "Isthmus Cingulate",
    "lateraloccipital": "Lateral Occipital",
    "lateralorbitofrontal": "Lateral Orbitofrontal",
    "lingual": "Lingual",
    "medialorbitofrontal": "Medial Orbitofrontal",
    "middletemporal": "Middle Temporal",
    "occipitalpole": "Occipital Pole",
    "paracentral": "Paracentral",
    "parahippocampal": "Parahippocampal",
    "parsopercularis": "Pars Opercularis",
    "parsorbitalis": "Pars Orbitalis",
    "parstriangularis": "Pars Triangularis",
    "pericalcarine": "Pericalcarine",
    "postcentral": "Postcentral",
    "posteriorcingulate": "Posterior Cingulate",
    "precentral": "Precentral",
    "precuneus": "Precuneus",
    "putamen": "Putamen",
    "rostralmiddlefrontal": "Rostral Middle Frontal",
    "superiorfrontal": "Superior Frontal",
    "superiorparietal": "Superior Parietal",
    "superiortemporal": "Superior Temporal",
    "supramarginal": "Supramarginal",
    "temporalpole": "Temporal Pole",
    "transversetemporal": "Transverse Temporal",
    "white matter": "White Matter",
}

_COMMON_SUBCORTICAL_LABELS: Dict[int, str] = {
    2: "Left Cerebral White Matter",
    3: "Left Cerebral Cortex",
    4: "Left Lateral Ventricle",
    5: "Left Inferior Lateral Ventricle",
    7: "Left Cerebellum White Matter",
    8: "Left Cerebellum Cortex",
    10: "Left Thalamus",
    11: "Left Caudate",
    12: "Left Putamen",
    13: "Left Pallidum",
    14: "Third Ventricle",
    15: "Fourth Ventricle",
    16: "Brain Stem",
    17: "Left Hippocampus",
    18: "Left Amygdala",
    24: "CSF",
    26: "Left Accumbens Area",
    28: "Left Ventral DC",
    30: "Left Vessel",
    31: "Left Choroid Plexus",
    41: "Right Cerebral White Matter",
    42: "Right Cerebral Cortex",
    43: "Right Lateral Ventricle",
    44: "Right Inferior Lateral Ventricle",
    46: "Right Cerebellum White Matter",
    47: "Right Cerebellum Cortex",
    49: "Right Thalamus",
    50: "Right Caudate",
    51: "Right Putamen",
    52: "Right Pallidum",
    53: "Right Hippocampus",
    54: "Right Amygdala",
    58: "Right Accumbens Area",
    60: "Right Ventral DC",
    62: "Right Vessel",
    63: "Right Choroid Plexus",
    77: "WM Hypointensities",
    251: "Corpus Callosum",
    252: "CC Posterior",
    253: "CC Mid Posterior",
    254: "CC Mid Anterior",
    255: "CC Anterior",
}


def _infer_hemisphere(label_id: int) -> Optional[str]:
    if 1000 <= label_id < 2000:
        return "left"
    if 2000 <= label_id < 3000:
        return "right"
    return None


def _humanize_anatomical_name(raw_name: str) -> str:
    name = (raw_name or "").strip()
    if not name:
        return ""

    lower = name.lower()
    for prefix in ("ctx-lh-", "ctx-rh-", "lh-", "rh-", "lh_", "rh_", "left-", "right-", "left_", "right_"):
        if lower.startswith(prefix):
            name = name[len(prefix):]
            lower = name.lower()
            break

    if lower in _LABEL_NAME_OVERRIDES:
        return _LABEL_NAME_OVERRIDES[lower]

    compact = re.sub(r"[ _-]+", " ", lower).strip()
    if not compact:
        return ""

    token_rewrites = [
        ("rostralmiddlefrontal", "Rostral Middle Frontal"),
        ("caudalmiddlefrontal", "Caudal Middle Frontal"),
        ("lateralorbitofrontal", "Lateral Orbitofrontal"),
        ("medialorbitofrontal", "Medial Orbitofrontal"),
        ("superiorparietal", "Superior Parietal"),
        ("superiorfrontal", "Superior Frontal"),
        ("superiortemporal", "Superior Temporal"),
        ("inferiorparietal", "Inferior Parietal"),
        ("inferiortemporal", "Inferior Temporal"),
        ("lateraloccipital", "Lateral Occipital"),
        ("posteriorcingulate", "Posterior Cingulate"),
        ("isthmuscingulate", "Isthmus Cingulate"),
        ("parstriangularis", "Pars Triangularis"),
        ("parsopercularis", "Pars Opercularis"),
        ("parsorbitalis", "Pars Orbitalis"),
        ("transversetemporal", "Transverse Temporal"),
        ("parahippocampal", "Parahippocampal"),
        ("frontalpole", "Frontal Pole"),
        ("temporalpole", "Temporal Pole"),
        ("occipitalpole", "Occipital Pole"),
        ("pericalcarine", "Pericalcarine"),
        ("precentral", "Precentral"),
        ("postcentral", "Postcentral"),
        ("supramarginal", "Supramarginal"),
        ("precuneus", "Precuneus"),
        ("paracentral", "Paracentral"),
        ("entorhinal", "Entorhinal"),
        ("bankssts", "Banks of Superior Temporal Sulcus"),
        ("calcarine", "Calcarine"),
        ("cuneus", "Cuneus"),
        ("lingual", "Lingual"),
        ("fusiform", "Fusiform"),
        ("insula", "Insula"),
    ]
    for needle, replacement in token_rewrites:
        if compact == needle:
            return replacement

    if " " in compact:
        return " ".join(part.capitalize() for part in compact.split())

    return compact.capitalize()


def _derive_label_record(label_id: int, raw_name: Optional[str]) -> Dict[str, Any]:
    raw = (raw_name or "").strip()
    base_name = _humanize_anatomical_name(raw)
    if not base_name or re.fullmatch(r"label-?\d+", base_name.lower()):
        base_name = _COMMON_SUBCORTICAL_LABELS.get(label_id, f"Region {label_id}")

    hemi = _infer_hemisphere(label_id)
    if hemi and label_id not in _COMMON_SUBCORTICAL_LABELS:
        base_name = f"{hemi.title()} {base_name}"

    aliases = []
    if raw and raw.lower() != base_name.lower():
        aliases.append(raw)
    aliases.append(f"Label {label_id}")
    aliases.append(f"{_slug(base_name)}-{label_id}")
    if hemi:
        aliases.insert(0, f"{hemi.title()} Hemisphere")

    cleaned_aliases: List[str] = []
    for alias in aliases:
        alias = str(alias).strip()
        if alias and alias not in cleaned_aliases:
            cleaned_aliases.append(alias)

    category = "subcortical_gray_matter"
    lowered = base_name.lower()
    if any(k in lowered for k in ("white matter", "corpus callosum")):
        category = "white_matter"
    elif any(k in lowered for k in ("ventricle", "csf", "choroid")):
        category = "csf_ventricular"
    elif any(k in lowered for k in ("cortex", "frontal", "temporal", "parietal", "occipital", "cingulate", "insula", "pole", "precuneus", "precentral", "postcentral", "supramarginal", "fusiform", "entorhinal", "parahippocampal", "banks of superior temporal sulcus")):
        category = "cortical_gray_matter"

    return {
        "id": label_id,
        "name": base_name,
        "raw_name": raw or None,
        "aliases": cleaned_aliases,
        "hemi": hemi,
        "category": category,
        "search_text": " ".join([base_name, raw, *cleaned_aliases]).strip(),
    }


def _parse_label_names_from_stats(subject_id: str, fastsurfer_dir: Path) -> Dict[int, str]:
    stats_candidates = [
        f"{subject_id}/stats/aseg+DKT.VINN.stats",
        f"{subject_id}/stats/aseg+DKT.VINN.withCC.stats",
        f"{subject_id}/stats/aseg.VINN.stats",
    ]
    names: Dict[int, str] = {}
    for candidate in stats_candidates:
        path = fastsurfer_dir / candidate
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) < 5:
                    continue
                try:
                    seg_id = int(parts[0])
                except ValueError:
                    continue
                if seg_id <= 0:
                    continue
                name = parts[4]
                if name and seg_id not in names:
                    names[seg_id] = name
        except Exception:
            continue
    return names


def _infer_lobe(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("frontal", "orbitofrontal", "precentral", "pars")):
        return "frontal"
    if any(k in n for k in ("temporal", "entorhinal", "parahippocampal", "fusiform", "bankssts", "transversetemporal")):
        return "temporal"
    if any(k in n for k in ("parietal", "postcentral", "precuneus", "supramarginal", "inferiorparietal", "superiorparietal")):
        return "parietal"
    if any(k in n for k in ("occipital", "cuneus", "calcarine", "lingual", "lateraloccipital", "pericalcarine")):
        return "occipital"
    if any(k in n for k in ("cingulate", "insula")):
        return "limbic_insular"
    if any(k in n for k in ("thalamus", "caudate", "putamen", "pallidum", "accumbens", "amygdala", "hippocampus")):
        return "subcortical"
    if any(k in n for k in ("vent", "csf", "wm", "white", "corpuscallosum")):
        return "ventricle_white_matter"
    return "other"


def _infer_network(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("precuneus", "posteriorcingulate", "medialorbitofrontal", "isthmuscingulate", "parahippocampal")):
        return "default_mode"
    if any(k in n for k in ("calcarine", "cuneus", "lingual", "lateraloccipital", "pericalcarine", "occipital")):
        return "visual"
    if any(k in n for k in ("precentral", "postcentral", "paracentral")):
        return "somatomotor"
    if any(k in n for k in ("superiorfrontal", "rostralmiddlefrontal", "caudalmiddlefrontal", "inferiorparietal")):
        return "frontoparietal"
    if any(k in n for k in ("supramarginal", "superiortemporal", "inferiortemporal", "middletemporal")):
        return "attention"
    if any(k in n for k in ("entorhinal", "temporalpole", "amygdala", "hippocampus", "insula")):
        return "limbic"
    return "subcortical_or_other"


def _infer_tissue(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("wm", "white", "corpuscallosum")):
        return "white_matter"
    if any(k in n for k in ("vent", "csf", "choroid")):
        return "csf_ventricular"
    if any(k in n for k in ("ctx", "cortex", "gyrus", "sulcus", "frontal", "temporal", "parietal", "occipital", "cingulate", "insula")):
        return "cortical_gray_matter"
    return "subcortical_gray_matter"


def get_mask_catalog(subject_id: str, fastsurfer_dir: Path) -> Dict[str, Any]:
    seg_file = _find_segmentation(subject_id, fastsurfer_dir)
    img = nib.load(str(seg_file))
    data = np.asarray(img.get_fdata()).astype(np.int32)
    unique_labels = sorted(int(v) for v in np.unique(data) if int(v) > 0)

    names = _parse_label_names_from_stats(subject_id, fastsurfer_dir)
    items: List[Dict[str, Any]] = []
    lobe_groups: Dict[str, List[int]] = {}
    network_groups: Dict[str, List[int]] = {}
    tissue_groups: Dict[str, List[int]] = {}
    for label_id in unique_labels:
        record = _derive_label_record(label_id, names.get(label_id))
        items.append(record)

        name = record["name"]

        lobe = _infer_lobe(name)
        network = _infer_network(name)
        tissue = _infer_tissue(name)
        lobe_groups.setdefault(lobe, []).append(label_id)
        network_groups.setdefault(network, []).append(label_id)
        tissue_groups.setdefault(tissue, []).append(label_id)

    for groups in (lobe_groups, network_groups, tissue_groups):
        for key in list(groups.keys()):
            groups[key] = sorted(set(groups[key]))

    return {
        "atlas_id": "fastsurfer_native",
        "subject_id": subject_id,
        "source_segmentation": str(seg_file),
        "items": items,
        "groups": {
            "by_lobe": lobe_groups,
            "by_network": network_groups,
            "by_tissue": tissue_groups,
        },
    }


def _build_mask(data: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
    include_labels = _as_int_list(params.get("include_labels", []))
    exclude_labels = _as_int_list(params.get("exclude_labels", []))

    if include_labels:
        mask = np.isin(data.astype(np.int32), include_labels)
    else:
        mask = data > 0

    if params.get("external_cortex_only", False):
        # FreeSurfer cortical labels are commonly >= 1000.
        mask &= data >= 1000

    if exclude_labels:
        mask &= ~np.isin(data.astype(np.int32), exclude_labels)

    if params.get("normalize_signal", False):
        data_float = data.astype(np.float32)
        den = float(np.max(data_float) - np.min(data_float))
        if den > 1e-8:
            data_norm = (data_float - float(np.min(data_float))) / den
            threshold = params.get("intensity_threshold")
            if threshold is not None:
                mask &= data_norm >= float(threshold)

    return mask


def _postprocess_mask(mask: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
    structure = generate_binary_structure(3, 2)
    open_iter = int(params.get("open_iterations", 0))
    close_iter = int(params.get("close_iterations", 0))

    if open_iter > 0:
        mask = binary_opening(mask, structure=structure, iterations=open_iter)
    if close_iter > 0:
        mask = binary_closing(mask, structure=structure, iterations=close_iter)

    min_component_voxels = int(params.get("min_component_voxels", 0))
    if min_component_voxels > 0:
        labeled, n = label(mask)
        if n > 0:
            counts = np.bincount(labeled.ravel())
            keep = np.where(counts >= min_component_voxels)[0]
            keep = keep[keep != 0]
            mask = np.isin(labeled, keep)

    return mask


def _mesh_from_mask(mask: np.ndarray, spacing: Iterable[float], params: Dict[str, Any]) -> tuple[trimesh.Trimesh, str]:
    verts, faces, _normals, _values = measure.marching_cubes(mask.astype(np.float32), level=0.5, spacing=tuple(spacing))
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)

    smooth_iter = int(params.get("smoothing_iterations", 0))
    smooth_lambda = float(params.get("smoothing_lambda", 0.45))
    if smooth_iter > 0:
        trimesh.smoothing.filter_taubin(mesh, lamb=smooth_lambda, nu=-0.53, iterations=smooth_iter)

    decimation_ratio = float(params.get("decimation_ratio", 1.0))
    if 0.0 < decimation_ratio < 1.0 and len(mesh.faces) > 64:
        target_faces = max(64, int(len(mesh.faces) * decimation_ratio))
        try:
            mesh = mesh.simplify_quadric_decimation(target_faces)
            return mesh, "quadric"
        except Exception:
            # Fallback path if the quadric simplification backend is unavailable.
            try:
                spacing_arr = np.asarray(tuple(spacing), dtype=np.float32)
                if decimation_ratio >= 0.75:
                    stride = 1
                elif decimation_ratio >= 0.45:
                    stride = 2
                elif decimation_ratio >= 0.25:
                    stride = 3
                else:
                    stride = 4
                reduced = mask[::stride, ::stride, ::stride]
                if np.count_nonzero(reduced) > 0 and min(reduced.shape) >= 3:
                    verts2, faces2, _n2, _v2 = measure.marching_cubes(
                        reduced.astype(np.float32),
                        level=0.5,
                        spacing=tuple(spacing_arr * stride),
                    )
                    mesh = trimesh.Trimesh(vertices=verts2, faces=faces2, process=False)
                    if smooth_iter > 0:
                        trimesh.smoothing.filter_taubin(mesh, lamb=smooth_lambda, nu=-0.53, iterations=smooth_iter)

                    mode = "voxel_downsample"
                    if len(mesh.faces) > target_faces:
                        thin_step = max(2, int(np.ceil(len(mesh.faces) / float(target_faces))))
                        mesh = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces[::thin_step], process=False)
                        mode = "voxel_downsample+face_thinning"
                    return mesh, mode
            except Exception:
                pass

            # Final fallback: deterministic face thinning to preserve a quality delta.
            thin_step = max(2, int(round(1.0 / max(decimation_ratio, 1e-3))))
            if thin_step > 1 and len(mesh.faces) > 64:
                mesh = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces[::thin_step], process=False)
                return mesh, "face_thinning"
            return mesh, "none"

    return mesh, "none"


def _generate_single_stl(
    *,
    subject_id: str,
    seg_file: Path,
    data: np.ndarray,
    output_dir: Path,
    preset: str,
    params: Dict[str, Any],
    run_stamp: str,
    suffix: Optional[str],
    selection_meta: Dict[str, Any],
) -> STLArtifact:
    manual_mask_path = str(params.get("manual_mask_path", "")).strip()
    if manual_mask_path:
        manual_img = nib.load(manual_mask_path)
        mask = np.asarray(manual_img.get_fdata()) > 0
        spacing = manual_img.header.get_zooms()[:3] if len(manual_img.header.get_zooms()) >= 3 else (1.0, 1.0, 1.0)
    else:
        mask = _build_mask(data, params)
        img = nib.load(str(seg_file))
        spacing = img.header.get_zooms()[:3] if len(img.header.get_zooms()) >= 3 else (1.0, 1.0, 1.0)

    mask = _postprocess_mask(mask, params)

    if int(np.count_nonzero(mask)) == 0:
        raise RuntimeError("STL generation produced an empty mask after filtering. Relax region/noise parameters.")

    mesh, decimation_mode = _mesh_from_mask(mask, spacing, params)

    # Topology repair for printability (best-effort, never fatal) + report.
    mesh_topo = None
    topology_repair = None
    try:
        from pipeline.topology import repair_mesh, mesh_topology
        mesh, topology_repair = repair_mesh(mesh)
        mesh_topo = mesh_topology(mesh)
    except Exception:
        pass

    subject_out = output_dir / subject_id
    subject_out.mkdir(parents=True, exist_ok=True)

    base = f"{subject_id}_{preset}_{run_stamp}"
    if suffix:
        base = f"{base}_{suffix}"
    stl_path = subject_out / f"{base}.stl"
    mesh.export(stl_path, file_type="stl")

    sidecar_path = subject_out / f"{base}.json"
    sidecar = {
        "subject_id": subject_id,
        "preset": preset,
        "params": params,
        "selection": selection_meta,
        "source_segmentation": str(seg_file),
        "source_manual_mask": manual_mask_path or None,
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "decimation_mode": decimation_mode,
        "requested_decimation_ratio": float(params.get("decimation_ratio", 1.0)),
        "mesh_topology": mesh_topo,
        "topology_repair": topology_repair,
        "output_stl": str(stl_path),
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    return STLArtifact(
        stl_path=stl_path,
        sidecar_path=sidecar_path,
        vertices=int(len(mesh.vertices)),
        faces=int(len(mesh.faces)),
        decimation_mode=decimation_mode,
    )


def generate_stl(
    *,
    subject_id: str,
    fastsurfer_dir: Path,
    output_dir: Path,
    preset: str,
    overrides: Dict[str, Any],
) -> STLResult:
    if preset not in PRESETS:
        raise ValueError(f"Unknown STL preset '{preset}'. Available: {sorted(PRESETS.keys())}")

    params: Dict[str, Any] = {**PRESETS[preset], **(overrides or {})}

    selected_labels = _as_int_list(params.get("selected_labels", []))
    if selected_labels and not params.get("include_labels"):
        params["include_labels"] = selected_labels

    if preset in {"by_region", "by_lobe", "by_network", "by_tissue"}:
        include_labels = _as_int_list(params.get("include_labels", []))
        if not include_labels:
            raise RuntimeError(
                "Automatic masking mode requires explicit selected labels. Pick regions/groups in the STL panel first."
            )

    seg_file = _find_segmentation(subject_id, fastsurfer_dir)
    img = nib.load(str(seg_file))
    data = np.asarray(img.get_fdata())

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    include_labels = _as_int_list(params.get("include_labels", []))
    split_by_label = bool(params.get("split_by_label", False))
    selected_items = params.get("selected_items", [])
    name_map: Dict[int, str] = {}
    if isinstance(selected_items, list):
        for entry in selected_items:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id")
            raw_name = entry.get("canonical_name") or entry.get("display_name") or entry.get("name")
            try:
                label_id = int(raw_id)
            except Exception:
                continue
            if isinstance(raw_name, str) and raw_name.strip():
                name_map[label_id] = raw_name.strip()

    artifacts: List[STLArtifact] = []
    if split_by_label and len(include_labels) > 1:
        for label_id in include_labels:
            entry_params = dict(params)
            entry_params["include_labels"] = [int(label_id)]
            label_name = name_map.get(int(label_id), f"Region {int(label_id)}")
            suffix = f"{_slug(label_name)}-{int(label_id)}"
            artifact = _generate_single_stl(
                subject_id=subject_id,
                seg_file=seg_file,
                data=data,
                output_dir=output_dir,
                preset=preset,
                params=entry_params,
                run_stamp=run_stamp,
                suffix=suffix,
                selection_meta={
                    "mode": params.get("mask_mode"),
                    "atlas_id": params.get("atlas_id"),
                    "selected_label": {"id": int(label_id), "name": label_name},
                    "selected_groups": params.get("selected_groups", []),
                },
            )
            artifacts.append(artifact)
    else:
        artifact = _generate_single_stl(
            subject_id=subject_id,
            seg_file=seg_file,
            data=data,
            output_dir=output_dir,
            preset=preset,
            params=params,
            run_stamp=run_stamp,
            suffix=None,
            selection_meta={
                "mode": params.get("mask_mode"),
                "atlas_id": params.get("atlas_id"),
                "selected_labels": include_labels,
                "selected_groups": params.get("selected_groups", []),
            },
        )
        artifacts.append(artifact)

    primary = artifacts[0]
    return STLResult(
        stl_path=primary.stl_path,
        sidecar_path=primary.sidecar_path,
        source_segmentation=seg_file,
        vertices=primary.vertices,
        faces=primary.faces,
        artifacts=artifacts,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--fastsurfer-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--preset", default="standard", choices=sorted(PRESETS.keys()))
    parser.add_argument("--params-json", default="{}")
    args = parser.parse_args()

    overrides = json.loads(args.params_json)
    result = generate_stl(
        subject_id=args.subject,
        fastsurfer_dir=args.fastsurfer_dir,
        output_dir=args.output_dir,
        preset=args.preset,
        overrides=overrides,
    )
    print(f"[stl] Wrote {result.stl_path}")
    print(f"[stl] Faces={result.faces} Vertices={result.vertices}")


if __name__ == "__main__":
    main()
