"""
neuro-orchestrator · Web Server
FastAPI + WebSocket -> browser-based pipeline control
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import nibabel as nib
import numpy as np
from scipy.ndimage import binary_dilation, binary_erosion, binary_fill_holes, label
import uvicorn
from fastapi import Body, FastAPI, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from pipeline.runner import PipelineRunner
from pipeline.persistence import SCHEMA_VERSION, load_checkpoint, save_checkpoint
from pipeline.state import PipelineState, STAGE_ORDER, STAGE_REQUIRES, StageStatus
from pipeline.tasks.stl_export import PRESETS as STL_PRESETS, generate_stl, get_mask_catalog
from pipeline.manifest import ArtifactManifest, ensure_dataset_description
from pipeline.adapters import register_stage_outputs
from pipeline.validators import validate_artifact, validate_and_record
from pipeline.progress import parse_progress
from pipeline.ingest import build_dcm2bids_command, write_default_config
from pipeline.group_stats import (
    compare_network_metrics,
    compare_fc_matrices,
    compare_fc_permutation,
    compare_fc_nbs,
    groups_from_participants,
    covariates_from_participants,
)
from utils.bids import scan_bids_dataset

# -- Config --------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/outputs"))
FS_LICENSE = Path(os.getenv("FS_LICENSE", "/licenses/license.txt"))
MOCK = os.getenv("MOCK_MODE", "0") == "1"
MASKS_DIRNAME = "masks"

# -- App -----------------------------------------------------------------------
app = FastAPI(title="neuro-orchestrator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# -- Global state --------------------------------------------------------------
pipeline_state = PipelineState()
runner = PipelineRunner(DATA_DIR, OUTPUT_DIR, FS_LICENSE, mock=MOCK)
# Canonical artifact ledger (BIDS-Derivatives root). Additive: records what each
# stage produced; downstream still uses existing discovery until the A2 swap.
manifest = ArtifactManifest(OUTPUT_DIR / "derivatives")
ensure_dataset_description(OUTPUT_DIR / "derivatives")
connections: List[WebSocket] = []
log_buffer: List[Dict[str, Any]] = []  # last 500 events
MAX_LOG_BUFFER = 500
LOG_CHECKPOINT_INTERVAL_SECONDS = 5.0
LOG_CHECKPOINT_BATCH_SIZE = 20
_pending_log_events = 0
_last_log_checkpoint_ts = 0.0


@dataclass
class STLJob:
    id: str
    subject_id: str
    preset: str
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending|running|completed|failed|cancelled
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    artifact_relpath: Optional[str] = None
    artifact_relpaths: List[str] = field(default_factory=list)


@dataclass
class STLIntent:
    key: str
    subject_id: str
    preset: str
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending|queued|completed|failed|cancelled
    created_at: str = ""
    updated_at: str = ""
    error: Optional[str] = None


stl_jobs: Dict[str, STLJob] = {}
stl_intents: Dict[str, STLIntent] = {}
stl_queue: asyncio.Queue[str] = asyncio.Queue()
stl_worker_task: Optional[asyncio.Task[Any]] = None

# Per-stage review gate policy, set at runtime from the run-setup screen.
#   mode:    auto  -> run headlessly, no pause
#            gated -> build the baseline, then pause for operator review (per trigger)
#            off   -> skip the stage entirely
#   trigger: always  -> always pause in gated mode
#            on_flag -> pause only when the baseline looks implausible
gate_config: Dict[str, Dict[str, str]] = {
    "mask": {"mode": "gated", "trigger": "always"},
}
# Append-only audit trail of gate decisions (doubles as provenance).
gate_audit: List[Dict[str, Any]] = []
# Cached QC summary per pending mask gate, so the reviewer sees metrics inline
# without recomputing on every snapshot broadcast. Cleared when the gate resolves.
mask_gate_details: Dict[str, Dict[str, Any]] = {}
# Live progress for the currently-running stage of each subject (parsed from the
# tool's streamed log). Ephemeral; cleared when the stage ends.
stage_progress: Dict[str, Dict[str, Any]] = {}
MASK_CACHE_MAX_ITEMS = 8
_mask_volume_cache: "OrderedDict[str, tuple[np.ndarray, nib.spatialimages.SpatialImage, Dict[str, Any]]]" = OrderedDict()
ANATOMY_CACHE_MAX_ITEMS = 4
_anatomy_volume_cache: "OrderedDict[str, np.ndarray]" = OrderedDict()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_checkpoint_payload() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "saved_at": _utc_now(),
        "state": pipeline_state.to_dict(),
        "logs": log_buffer[-MAX_LOG_BUFFER:],
        "stl_intents": [asdict(i) for i in stl_intents.values()],
        "gate_config": gate_config,
        "gate_audit": gate_audit[-MAX_LOG_BUFFER:],
    }


def _save_checkpoint_now() -> None:
    global _pending_log_events, _last_log_checkpoint_ts
    save_checkpoint(OUTPUT_DIR, _build_checkpoint_payload())
    _pending_log_events = 0
    _last_log_checkpoint_ts = time.time()


def _maybe_flush_logs_to_checkpoint(force: bool = False) -> None:
    global _pending_log_events
    if force:
        _save_checkpoint_now()
        return
    now = time.time()
    if (
        _pending_log_events >= LOG_CHECKPOINT_BATCH_SIZE
        or (now - _last_log_checkpoint_ts) >= LOG_CHECKPOINT_INTERVAL_SECONDS
    ):
        _save_checkpoint_now()


def _scan_subject_modalities() -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for sub in scan_bids_dataset(DATA_DIR):
        out[sub.id] = set(sub.modalities)
    return out


def _append_interruption_logs(interrupted: List[tuple[str, str]]) -> None:
    for subject_id, stage in interrupted:
        log_buffer.append({
            "type": "log",
            "subject_id": subject_id,
            "stage": stage,
            "message": (
                f"[resume] Previous run interrupted during {stage}; "
                "restored as failed for safe resume."
            ),
            "level": "error",
        })
    if len(log_buffer) > MAX_LOG_BUFFER:
        del log_buffer[:-MAX_LOG_BUFFER]


def _restore_checkpoint() -> Dict[str, int]:
    global pipeline_state, log_buffer, stl_intents, gate_config, gate_audit
    restored_subjects = 0
    restored_logs = 0
    interrupted_count = 0

    checkpoint = load_checkpoint(OUTPUT_DIR)
    if checkpoint:
        raw_gate_config = checkpoint.get("gate_config")
        if isinstance(raw_gate_config, dict):
            for stage, cfg in raw_gate_config.items():
                if isinstance(cfg, dict) and stage in STAGE_ORDER:
                    gate_config[stage] = {
                        "mode": str(cfg.get("mode", "auto")),
                        "trigger": str(cfg.get("trigger", "always")),
                    }
        raw_gate_audit = checkpoint.get("gate_audit")
        if isinstance(raw_gate_audit, list):
            gate_audit = [e for e in raw_gate_audit if isinstance(e, dict)][-MAX_LOG_BUFFER:]
        state_data = checkpoint.get("state", {})
        if isinstance(state_data, dict):
            pipeline_state = PipelineState.from_dict(state_data)
            restored_subjects = len(pipeline_state.subjects)

        raw_logs = checkpoint.get("logs", [])
        if isinstance(raw_logs, list):
            log_buffer = [entry for entry in raw_logs if isinstance(entry, dict)][-MAX_LOG_BUFFER:]
            restored_logs = len(log_buffer)

        raw_intents = checkpoint.get("stl_intents", [])
        if isinstance(raw_intents, list):
            restored_intents: Dict[str, STLIntent] = {}
            for item in raw_intents:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "")).strip()
                subject_id = str(item.get("subject_id", "")).strip()
                preset = str(item.get("preset", "")).strip()
                if not key or not subject_id or not preset:
                    continue
                restored_intents[key] = STLIntent(
                    key=key,
                    subject_id=subject_id,
                    preset=preset,
                    params=dict(item.get("params", {})),
                    status=str(item.get("status", "pending")),
                    created_at=str(item.get("created_at", "")),
                    updated_at=str(item.get("updated_at", "")),
                    error=item.get("error"),
                )
            stl_intents = restored_intents

    pipeline_state.reconcile_with_scan(_scan_subject_modalities())
    interrupted = pipeline_state.mark_interrupted_running_as_failed()
    interrupted_count = len(interrupted)
    if interrupted:
        _append_interruption_logs(interrupted)

    _save_checkpoint_now()
    return {
        "restored_subjects": restored_subjects,
        "restored_logs": restored_logs,
        "interrupted": interrupted_count,
        "active_subjects": len(pipeline_state.subjects),
    }


def _reload_subjects() -> None:
    pipeline_state.reconcile_with_scan(_scan_subject_modalities())
    active = set(pipeline_state.subjects.keys())
    stale = [k for k, i in stl_intents.items() if i.subject_id not in active]
    for key in stale:
        stl_intents.pop(key, None)


_reload_subjects()


# -- Broadcast helpers ---------------------------------------------------------
async def broadcast(msg: Dict[str, Any]) -> None:
    global _pending_log_events
    if msg.get("type") != "state_snapshot":
        log_buffer.append(msg)
        if len(log_buffer) > MAX_LOG_BUFFER:
            del log_buffer[:-MAX_LOG_BUFFER]
        _pending_log_events += 1
        _maybe_flush_logs_to_checkpoint()
    txt = json.dumps(msg)
    dead = []
    for ws in connections:
        try:
            await ws.send_text(txt)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in connections:
            connections.remove(ws)


def _list_subject_artifacts(subject_id: str) -> List[Dict[str, Any]]:
    stl_dir = OUTPUT_DIR / "stl" / subject_id
    if not stl_dir.exists():
        return []

    stl_files = sorted(
        stl_dir.glob("*.stl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out: List[Dict[str, Any]] = []
    for stl_file in stl_files:
        rel = stl_file.relative_to(OUTPUT_DIR).as_posix()
        quality: Dict[str, Any] = {}
        sidecar = stl_file.with_suffix(".json")
        if sidecar.exists():
            try:
                sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
                quality = {
                    "preset": sidecar_data.get("preset"),
                    "faces": sidecar_data.get("faces"),
                    "vertices": sidecar_data.get("vertices"),
                    "decimation_mode": sidecar_data.get("decimation_mode"),
                    "requested_decimation_ratio": sidecar_data.get("requested_decimation_ratio"),
                    "selection": sidecar_data.get("selection"),
                }
            except Exception:
                quality = {}
        out.append({
            "name": stl_file.name,
            "relative_path": rel,
            "url": f"/artifacts/{rel}",
            "size_bytes": stl_file.stat().st_size,
            "quality": quality,
        })
    return out


def _remove_stl_subject_outputs(subject_id: str) -> Dict[str, int]:
    stl_dir = OUTPUT_DIR / "stl" / subject_id
    removed = {"stl": 0, "json": 0}
    if not stl_dir.exists():
        return removed

    for stl_file in stl_dir.glob("*.stl"):
        try:
            stl_file.unlink(missing_ok=True)
            removed["stl"] += 1
        except Exception:
            pass
        sidecar = stl_file.with_suffix(".json")
        if sidecar.exists():
            try:
                sidecar.unlink(missing_ok=True)
                removed["json"] += 1
            except Exception:
                pass

    try:
        for extra in stl_dir.glob("*.json"):
            try:
                extra.unlink(missing_ok=True)
                removed["json"] += 1
            except Exception:
                pass
    except Exception:
        pass

    try:
        if stl_dir.exists() and not any(stl_dir.iterdir()):
            stl_dir.rmdir()
    except Exception:
        pass

    return removed


def _mask_root() -> Path:
    return OUTPUT_DIR / MASKS_DIRNAME


def _mask_subject_dir(subject_id: str) -> Path:
    return _mask_root() / subject_id


def _mask_versions_dir(subject_id: str) -> Path:
    return _mask_subject_dir(subject_id) / "versions"


def _mask_version_nifti_path(subject_id: str, version_id: str) -> Path:
    return _mask_versions_dir(subject_id) / f"{version_id}.nii.gz"


def _mask_version_sidecar_path(subject_id: str, version_id: str) -> Path:
    return _mask_versions_dir(subject_id) / f"{version_id}.json"


def _mask_cache_key(subject_id: str, version_id: str) -> str:
    return f"{subject_id}:{version_id}"


def _mask_cache_invalidate(subject_id: str, version_id: Optional[str] = None) -> None:
    if version_id:
        _mask_volume_cache.pop(_mask_cache_key(subject_id, version_id), None)
        return
    prefix = f"{subject_id}:"
    stale_keys = [k for k in _mask_volume_cache.keys() if k.startswith(prefix)]
    for key in stale_keys:
        _mask_volume_cache.pop(key, None)


def _to_output_relative(path: Path) -> str:
    return path.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()


def _load_version_meta(subject_id: str, version_id: str) -> Dict[str, Any]:
    sidecar = _mask_version_sidecar_path(subject_id, version_id)
    if not sidecar.exists():
        raise FileNotFoundError(f"Mask version metadata not found: {sidecar}")
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Invalid mask version sidecar format")
    return raw


def _list_mask_versions(subject_id: str) -> List[Dict[str, Any]]:
    versions_dir = _mask_versions_dir(subject_id)
    if not versions_dir.exists():
        return []

    versions: List[Dict[str, Any]] = []
    for sidecar in sorted(versions_dir.glob("*.json")):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                continue
            version_id = str(meta.get("version_id", "")).strip() or sidecar.stem
            nifti = _mask_version_nifti_path(subject_id, version_id)
            if not nifti.exists():
                continue
            rel = _to_output_relative(nifti)
            versions.append({
                **meta,
                "version_id": version_id,
                "relative_path": rel,
                "url": f"/artifacts/{rel}",
            })
        except Exception:
            continue

    versions.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    return versions


def _latest_mask_version_id(subject_id: str) -> Optional[str]:
    versions = _list_mask_versions(subject_id)
    if not versions:
        return None
    latest = str(versions[0].get("version_id", "")).strip()
    return latest or None


def _save_mask_version(
    *,
    subject_id: str,
    mask: np.ndarray,
    reference_img: nib.spatialimages.SpatialImage,
    parent_version_id: Optional[str],
    source_type: str,
    operation_summary: str,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    versions_dir = _mask_versions_dir(subject_id)
    versions_dir.mkdir(parents=True, exist_ok=True)

    version_id = datetime.now(timezone.utc).strftime("v%Y%m%dT%H%M%S%fZ")
    nifti_path = _mask_version_nifti_path(subject_id, version_id)
    sidecar_path = _mask_version_sidecar_path(subject_id, version_id)

    mask_u8 = (mask > 0).astype(np.uint8)
    out_img = nib.Nifti1Image(mask_u8, reference_img.affine, reference_img.header)
    nib.save(out_img, str(nifti_path))

    meta: Dict[str, Any] = {
        "version_id": version_id,
        "subject_id": subject_id,
        "created_at": _utc_now(),
        "parent_version_id": parent_version_id,
        "source_type": source_type,
        "operation_summary": operation_summary,
        "voxel_count": int(np.count_nonzero(mask_u8)),
        "shape": [int(v) for v in mask_u8.shape],
    }
    if extra_meta:
        meta.update(extra_meta)
    sidecar_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    rel = _to_output_relative(nifti_path)
    return {
        **meta,
        "relative_path": rel,
        "url": f"/artifacts/{rel}",
    }


def _load_mask_version_array(subject_id: str, version_id: str) -> tuple[np.ndarray, nib.spatialimages.SpatialImage, Dict[str, Any]]:
    cache_key = _mask_cache_key(subject_id, version_id)
    cached = _mask_volume_cache.get(cache_key)
    if cached is not None:
        _mask_volume_cache.move_to_end(cache_key)
        return cached

    nifti = _mask_version_nifti_path(subject_id, version_id)
    if not nifti.exists():
        raise FileNotFoundError(f"Mask version not found: {version_id}")
    img = nib.load(str(nifti))
    data = np.asarray(img.get_fdata())
    mask = data > 0
    meta = _load_version_meta(subject_id, version_id)

    entry = (mask, img, meta)
    _mask_volume_cache[cache_key] = entry
    _mask_volume_cache.move_to_end(cache_key)
    while len(_mask_volume_cache) > MASK_CACHE_MAX_ITEMS:
        _mask_volume_cache.popitem(last=False)
    return entry


def _extract_mask_slice(mask: np.ndarray, plane: str, index: Optional[int]) -> Dict[str, Any]:
    plane_norm = plane.strip().lower()
    if plane_norm not in {"axial", "coronal", "sagittal"}:
        raise ValueError("plane must be one of: axial, coronal, sagittal")

    axis = 0 if plane_norm == "axial" else (1 if plane_norm == "coronal" else 2)
    max_index = int(mask.shape[axis] - 1)
    idx = max_index // 2 if index is None else int(index)
    idx = max(0, min(max_index, idx))

    if axis == 0:
        sl = mask[idx, :, :]
    elif axis == 1:
        sl = mask[:, idx, :]
    else:
        sl = mask[:, :, idx]

    slice_u8 = sl.astype(np.uint8)
    return {
        "plane": plane_norm,
        "index": idx,
        "max_index": max_index,
        "width": int(slice_u8.shape[1]),
        "height": int(slice_u8.shape[0]),
        "data": [int(v) for v in slice_u8.ravel()],
    }


def _extract_intensity_slice(volume: np.ndarray, plane: str, index: Optional[int]) -> Dict[str, Any]:
    plane_norm = plane.strip().lower()
    if plane_norm not in {"axial", "coronal", "sagittal"}:
        raise ValueError("plane must be one of: axial, coronal, sagittal")

    axis = 0 if plane_norm == "axial" else (1 if plane_norm == "coronal" else 2)
    max_index = int(volume.shape[axis] - 1)
    idx = max_index // 2 if index is None else int(index)
    idx = max(0, min(max_index, idx))

    if axis == 0:
        sl = volume[idx, :, :]
    elif axis == 1:
        sl = volume[:, idx, :]
    else:
        sl = volume[:, :, idx]

    sl = np.asarray(sl, dtype=np.float32)
    finite_vals = sl[np.isfinite(sl)]
    if finite_vals.size == 0:
        norm_u8 = np.zeros_like(sl, dtype=np.uint8)
    else:
        lo = float(np.percentile(finite_vals, 2))
        hi = float(np.percentile(finite_vals, 98))
        if hi <= lo:
            hi = lo + 1e-6
        norm = np.clip((sl - lo) / (hi - lo), 0.0, 1.0)
        norm_u8 = (norm * 255.0).astype(np.uint8)

    return {
        "plane": plane_norm,
        "index": idx,
        "max_index": max_index,
        "width": int(norm_u8.shape[1]),
        "height": int(norm_u8.shape[0]),
        "data": [int(v) for v in norm_u8.ravel()],
    }


def _resolve_auto_labels(subject_id: str, preset: str, params: Dict[str, Any]) -> List[int]:
    selected_labels = params.get("selected_labels", params.get("include_labels", []))
    selected_groups = params.get("selected_groups", [])
    labels: List[int] = []

    if isinstance(selected_labels, list):
        for label_id in selected_labels:
            try:
                labels.append(int(label_id))
            except Exception:
                continue

    if not labels and isinstance(selected_groups, list) and selected_groups and preset in {"by_region", "by_lobe", "by_network", "by_tissue"}:
        catalog = get_mask_catalog(subject_id, OUTPUT_DIR / "fastsurfer")
        labels = _resolve_group_labels(catalog, preset, selected_groups)

    return sorted(set(labels))


def _build_auto_mask(subject_id: str, preset: str, params: Dict[str, Any]) -> tuple[np.ndarray, nib.spatialimages.SpatialImage, Dict[str, Any]]:
    seg_path = _find_fastsurfer_segmentation(subject_id)
    if not seg_path:
        raise RuntimeError(_stl_prereq_error(subject_id) or f"No segmentation found for {subject_id}")

    seg_img = nib.load(str(seg_path))
    seg = np.asarray(seg_img.get_fdata()).astype(np.int32)
    include_labels = _resolve_auto_labels(subject_id, preset, params)

    if include_labels:
        mask = np.isin(seg, include_labels)
    else:
        mask = seg > 0

    if bool(params.get("external_cortex_only", False)):
        mask &= seg >= 1000

    exclude_labels = params.get("exclude_labels", [])
    if isinstance(exclude_labels, list) and exclude_labels:
        parsed_excludes: List[int] = []
        for value in exclude_labels:
            try:
                parsed_excludes.append(int(value))
            except Exception:
                continue
        if parsed_excludes:
            mask &= ~np.isin(seg, parsed_excludes)

    info = {
        "source_segmentation": str(seg_path),
        "preset": preset,
        "mask_mode": params.get("mask_mode"),
        "selected_labels": include_labels,
        "selected_groups": params.get("selected_groups", []),
    }
    return mask, seg_img, info


def _find_subject_t1_image(subject_id: str) -> Optional[Path]:
    anat_dir = DATA_DIR / subject_id / "anat"
    if not anat_dir.is_dir():
        return None
    candidates = sorted(anat_dir.glob(f"{subject_id}_*_T1w.nii.gz"))
    if not candidates:
        candidates = sorted(anat_dir.glob(f"{subject_id}_*_T1w.nii"))
    if not candidates:
        candidates = sorted(anat_dir.glob("*_T1w.nii.gz"))
    if not candidates:
        candidates = sorted(anat_dir.glob("*_T1w.nii"))
    return candidates[0] if candidates else None


def _resolve_mask_reference_image(subject_id: str) -> tuple[nib.spatialimages.SpatialImage, Dict[str, Any]]:
    seg_path = _find_fastsurfer_segmentation(subject_id)
    if seg_path:
        img = nib.load(str(seg_path))
        return img, {
            "reference_type": "fastsurfer_segmentation",
            "reference_path": str(seg_path),
        }

    t1_path = _find_subject_t1_image(subject_id)
    if t1_path:
        img = nib.load(str(t1_path))
        return img, {
            "reference_type": "t1w",
            "reference_path": str(t1_path),
        }

    raise RuntimeError(
        "No reference image available for empty mask init. "
        "Expected a FastSurfer segmentation or subject T1w image under /data/{subject_id}/anat."
    )


def _build_empty_mask(subject_id: str) -> tuple[np.ndarray, nib.spatialimages.SpatialImage, Dict[str, Any]]:
    ref_img, ref_info = _resolve_mask_reference_image(subject_id)
    shape = tuple(int(v) for v in ref_img.shape[:3])
    if len(shape) != 3:
        raise RuntimeError(f"Unsupported reference image shape for empty mask: {ref_img.shape}")
    mask = np.zeros(shape, dtype=bool)
    return mask, ref_img, ref_info


def _load_reference_anatomy_volume(subject_id: str) -> np.ndarray:
    cached = _anatomy_volume_cache.get(subject_id)
    if cached is not None:
        _anatomy_volume_cache.move_to_end(subject_id)
        return cached

    ref_img, _ref_info = _resolve_mask_reference_image(subject_id)
    data = np.asarray(ref_img.get_fdata(), dtype=np.float32)
    if data.ndim < 3:
        raise RuntimeError(f"Reference image has invalid dimensions: {data.shape}")
    if data.ndim > 3:
        data = data[..., 0]

    _anatomy_volume_cache[subject_id] = data
    _anatomy_volume_cache.move_to_end(subject_id)
    while len(_anatomy_volume_cache) > ANATOMY_CACHE_MAX_ITEMS:
        _anatomy_volume_cache.popitem(last=False)
    return data


def _sphere_indices(shape: tuple[int, int, int], center: tuple[int, int, int], radius: int) -> np.ndarray:
    cz, cy, cx = center
    r = max(1, int(radius))
    z_min = max(0, cz - r)
    z_max = min(shape[0] - 1, cz + r)
    y_min = max(0, cy - r)
    y_max = min(shape[1] - 1, cy + r)
    x_min = max(0, cx - r)
    x_max = min(shape[2] - 1, cx + r)

    zz, yy, xx = np.ogrid[z_min : z_max + 1, y_min : y_max + 1, x_min : x_max + 1]
    dist2 = (zz - cz) ** 2 + (yy - cy) ** 2 + (xx - cx) ** 2
    return dist2 <= (r * r)


def _apply_mask_operation(mask: np.ndarray, operation: Dict[str, Any]) -> tuple[np.ndarray, str]:
    op_type = str(operation.get("type", "")).strip().lower()
    if not op_type:
        raise ValueError("operation.type is required")

    out = np.array(mask, dtype=bool, copy=True)
    if op_type in {"paint", "erase"}:
        center = operation.get("center")
        if not (isinstance(center, list) and len(center) == 3):
            raise ValueError("paint/erase requires center=[z,y,x]")
        try:
            cz, cy, cx = int(center[0]), int(center[1]), int(center[2])
        except Exception as exc:
            raise ValueError(f"invalid center coordinates: {exc}")
        radius = int(operation.get("radius", 1))
        if not (0 <= cz < out.shape[0] and 0 <= cy < out.shape[1] and 0 <= cx < out.shape[2]):
            raise ValueError("center is out of mask bounds")

        local = _sphere_indices(out.shape, (cz, cy, cx), radius)
        z_min = max(0, cz - radius)
        y_min = max(0, cy - radius)
        x_min = max(0, cx - radius)
        view = out[z_min : z_min + local.shape[0], y_min : y_min + local.shape[1], x_min : x_min + local.shape[2]]
        if op_type == "paint":
            view[local] = True
        else:
            view[local] = False
        summary = f"{op_type} r={radius} center=[{cz},{cy},{cx}]"
        return out, summary

    iterations = int(operation.get("iterations", 1))
    iterations = max(1, iterations)

    if op_type == "grow":
        out = binary_dilation(out, iterations=iterations)
        return out, f"grow iterations={iterations}"
    if op_type == "shrink":
        out = binary_erosion(out, iterations=iterations)
        return out, f"shrink iterations={iterations}"
    if op_type == "fill_holes":
        out = binary_fill_holes(out)
        return out, "fill_holes"
    if op_type in {"keep_largest", "largest_component"}:
        labeled, n = label(out)
        if n <= 0:
            return out, "keep_largest no-op (empty mask)"
        counts = np.bincount(labeled.ravel())
        counts[0] = 0
        largest = int(np.argmax(counts))
        out = labeled == largest
        return out, "keep_largest_component"
    if op_type in {"repair_topology", "make_printable"}:
        # One-click topology cleanup: largest component + fill enclosed cavities,
        # for a watertight, genus-reduced, printable mask.
        from pipeline.topology import repair_mask_topology
        repaired, report = repair_mask_topology(out)
        out = np.asarray(repaired, dtype=bool)
        summary = (
            f"repair_topology removed_components={report.get('removed_components', 0)} "
            f"filled_cavities={report.get('filled_cavities', 0)}"
        )
        return out, summary

    raise ValueError(f"Unsupported operation.type '{op_type}'")


def _purge_stl_runtime_state(subject_id: Optional[str] = None) -> Dict[str, int]:
    removed_jobs = 0
    removed_intents = 0

    if subject_id is None:
        removed_jobs = len(stl_jobs)
        removed_intents = len(stl_intents)
        stl_jobs.clear()
        stl_intents.clear()
        return {"jobs": removed_jobs, "intents": removed_intents}

    for job_id, job in list(stl_jobs.items()):
        if job.subject_id == subject_id:
            stl_jobs.pop(job_id, None)
            removed_jobs += 1

    for intent_key, intent in list(stl_intents.items()):
        if intent.subject_id == subject_id:
            stl_intents.pop(intent_key, None)
            removed_intents += 1

    return {"jobs": removed_jobs, "intents": removed_intents}


def _find_fastsurfer_segmentation(subject_id: str) -> Optional[Path]:
    # Prefer the canonical artifact (decoupled from which seg tool ran); fall back
    # to globbing for older runs registered before the manifest existed.
    resolved = manifest.resolve_path(subject_id, "seg")
    if resolved and resolved.is_file():
        return resolved

    fastsurfer_dir = OUTPUT_DIR / "fastsurfer"
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
    return None


def _stl_prereq_error(subject_id: str) -> Optional[str]:
    sub = pipeline_state.subjects.get(subject_id)
    if not sub:
        return "Subject not found"

    fastsurfer_status = sub.stage_status.get("fastsurfer")
    if fastsurfer_status == StageStatus.SKIPPED:
        return f"FastSurfer is skipped for {subject_id} (missing required modalities)."
    if fastsurfer_status == StageStatus.FAILED:
        return f"FastSurfer failed for {subject_id}. Rerun FastSurfer before generating STL."
    if fastsurfer_status != StageStatus.COMPLETED:
        return (
            f"FastSurfer is not completed for {subject_id}. "
            "Run FastSurfer first and wait until it finishes before generating STL."
        )

    seg = _find_fastsurfer_segmentation(subject_id)
    if not seg:
        return (
            f"FastSurfer finished state is present, but no segmentation file was found for {subject_id} under "
            f"{OUTPUT_DIR / 'fastsurfer'}."
        )

    return None


def _normalize_stl_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    preset = str(payload.get("preset", "standard"))
    params = dict(payload.get("params", {}))
    if preset not in STL_PRESETS:
        raise HTTPException(status_code=400, detail=f"Invalid preset '{preset}'")

    if "selected_labels" in payload and "selected_labels" not in params:
        params["selected_labels"] = payload.get("selected_labels")
    if "selected_groups" in payload and "selected_groups" not in params:
        params["selected_groups"] = payload.get("selected_groups")
    if "selected_items" in payload and "selected_items" not in params:
        params["selected_items"] = payload.get("selected_items")
    if "atlas_id" in payload and "atlas_id" not in params:
        params["atlas_id"] = payload.get("atlas_id")

    auto_modes = {"by_region", "by_lobe", "by_network", "by_tissue"}
    if preset in auto_modes:
        params.setdefault("mask_mode", preset)
        selected_labels = params.get("selected_labels", params.get("include_labels", []))
        selected_groups = params.get("selected_groups", [])
        if selected_labels:
            try:
                params["selected_labels"] = [int(v) for v in selected_labels]
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"selected_labels must be integers: {exc}")
        if selected_groups and not isinstance(selected_groups, list):
            raise HTTPException(status_code=400, detail="selected_groups must be a list of group names")
        if not params.get("selected_labels") and not selected_groups:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Automatic masking requires explicit selection. "
                    "Choose regions/lobes/networks/tissues before submitting STL."
                ),
            )
    return {"preset": preset, "params": params}


def _resolve_group_labels(catalog: Dict[str, Any], mode: str, selected_groups: List[str]) -> List[int]:
    groups = catalog.get("groups", {}) if isinstance(catalog, dict) else {}
    mode_groups = groups.get(mode, {}) if isinstance(groups, dict) else {}
    labels: List[int] = []
    for group_name in selected_groups:
        group_labels = mode_groups.get(group_name)
        if not isinstance(group_labels, list):
            continue
        for label in group_labels:
            try:
                labels.append(int(label))
            except Exception:
                continue
    return sorted(set(labels))


def _stl_intent_key(subject_id: str, preset: str, params: Dict[str, Any]) -> str:
    params_key = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return f"{subject_id}|{preset}|{params_key}"


def _upsert_stl_intent(subject_id: str, payload: Dict[str, Any]) -> STLIntent:
    normalized = _normalize_stl_payload(payload)
    preset = normalized["preset"]
    params = normalized["params"]

    if preset in ("by_region", "by_lobe", "by_network", "by_tissue"):
        selected_labels = params.get("selected_labels", params.get("include_labels", []))
        selected_groups = params.get("selected_groups", [])
        if (not selected_labels) and isinstance(selected_groups, list) and selected_groups:
            catalog = get_mask_catalog(subject_id, OUTPUT_DIR / "fastsurfer")
            selected_labels = _resolve_group_labels(catalog, preset, selected_groups)
        if selected_labels:
            params["selected_labels"] = [int(v) for v in selected_labels]
            params["include_labels"] = [int(v) for v in selected_labels]
        if preset == "by_region":
            params.setdefault("split_by_label", True)

    key = _stl_intent_key(subject_id, preset, params)
    now = _utc_now()
    existing = stl_intents.get(key)
    if existing and existing.status in ("pending", "queued"):
        existing.updated_at = now
        return existing

    intent = STLIntent(
        key=key,
        subject_id=subject_id,
        preset=preset,
        params=params,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    stl_intents[key] = intent
    return intent


async def _process_stl_intents_for_subject(subject_id: str) -> None:
    pending = [
        i for i in stl_intents.values()
        if i.subject_id == subject_id and i.status == "pending"
    ]
    if not pending:
        return

    for intent in pending:
        prereq_error = _stl_prereq_error(subject_id)
        if prereq_error:
            intent.updated_at = _utc_now()
            # Keep pending while FastSurfer is still in progress.
            if "not completed" in prereq_error.lower():
                continue
            intent.status = "failed"
            intent.error = prereq_error
            await broadcast({
                "type": "log",
                "subject_id": subject_id,
                "stage": "stl",
                "message": f"[stl] Deferred intent failed: {prereq_error}",
                "level": "error",
            })
            continue

        job = _queue_stl_job(subject_id, {"preset": intent.preset, "params": intent.params})
        intent.status = "queued"
        intent.updated_at = _utc_now()
        await broadcast({
            "type": "log",
            "subject_id": subject_id,
            "stage": "stl",
            "message": f"[stl] Deferred intent queued job {job.id[:8]} (preset={job.preset})",
            "level": "stage",
        })

    _save_checkpoint_now()
    await broadcast(_snapshot())


def _start_fastsurfer_if_possible(subject_id: str) -> str:
    sub = pipeline_state.subjects.get(subject_id)
    if not sub:
        return "subject_missing"
    status = sub.stage_status.get("fastsurfer")
    if status == StageStatus.SKIPPED:
        return "skipped"
    if status == StageStatus.COMPLETED:
        return "completed"
    if sub.overall_status == StageStatus.RUNNING:
        return "already_running"
    if status in (StageStatus.PENDING, StageStatus.FAILED):
        asyncio.create_task(_run(subject_id, stages=["fastsurfer"]))
        return "started"
    return "deferred"


def _snapshot() -> Dict[str, Any]:
    subjects: Dict[str, Any] = {}
    for sid, sub in pipeline_state.subjects.items():
        done, total = sub.progress
        subjects[sid] = {
            "id": sid,
            "modalities": sorted(sub.modalities),
            "stages": {s: sub.stage_status[s].value for s in STAGE_ORDER if s in sub.stage_status},
            "overall": sub.overall_status.value,
            "progress": {"done": done, "total": total},
            "current_stage": sub.current_stage,
            "artifacts": _list_subject_artifacts(sid),
            "live": stage_progress.get(sid),
        }

    queue_order = list(stl_queue._queue) if hasattr(stl_queue, "_queue") else []
    jobs = [asdict(j) for j in sorted(stl_jobs.values(), key=lambda x: x.created_at, reverse=True)]
    intents = [asdict(i) for i in sorted(stl_intents.values(), key=lambda x: x.created_at, reverse=True)]

    pending_gates = []
    for sid, sub in pipeline_state.subjects.items():
        for stage in STAGE_ORDER:
            if sub.stage_status.get(stage) != StageStatus.AWAITING_REVIEW:
                continue
            entry: Dict[str, Any] = {"subject_id": sid, "stage": stage}
            if stage == "mask":
                detail = _mask_gate_detail(sid)
                if detail:
                    entry["detail"] = detail
            pending_gates.append(entry)

    return {
        "type": "state_snapshot",
        "subjects": subjects,
        "mock": MOCK,
        "stl": {
            "presets": sorted(STL_PRESETS.keys()),
            "jobs": jobs,
            "intents": intents,
            "queue": queue_order,
        },
        "gates": {
            "config": gate_config,
            "pending": pending_gates,
            "audit": gate_audit[-25:],
        },
    }


# -- STL worker ----------------------------------------------------------------
async def _run_stl_worker() -> None:
    while True:
        job_id = await stl_queue.get()
        job = stl_jobs.get(job_id)
        if not job:
            stl_queue.task_done()
            continue

        if job.status == "cancelled":
            stl_queue.task_done()
            await broadcast(_snapshot())
            continue

        job.status = "running"
        job.started_at = _utc_now()
        await broadcast({
            "type": "log",
            "subject_id": job.subject_id,
            "stage": "stl",
            "message": f"[stl] Job {job.id[:8]} started with preset={job.preset}",
            "level": "stage",
        })
        await broadcast(_snapshot())

        try:
            result = await asyncio.to_thread(
                generate_stl,
                subject_id=job.subject_id,
                fastsurfer_dir=OUTPUT_DIR / "fastsurfer",
                output_dir=OUTPUT_DIR / "stl",
                preset=job.preset,
                overrides=job.params,
            )
            rel = result.stl_path.relative_to(OUTPUT_DIR).as_posix()
            rels = [a.stl_path.relative_to(OUTPUT_DIR).as_posix() for a in result.artifacts]
            job.status = "completed"
            job.artifact_relpath = rel
            job.artifact_relpaths = rels
            try:
                manifest.register(
                    subject=job.subject_id, role="stl", path=result.stl_path,
                    stage="mask", tool="built-in mesher",
                    inputs=manifest.input_refs(job.subject_id, ["mask_version"]),
                )
            except Exception:
                pass
            for intent in stl_intents.values():
                if (
                    intent.subject_id == job.subject_id
                    and intent.preset == job.preset
                    and intent.params == job.params
                    and intent.status == "queued"
                ):
                    intent.status = "completed"
                    intent.updated_at = _utc_now()
                    intent.error = None
            await broadcast({
                "type": "log",
                "subject_id": job.subject_id,
                "stage": "stl",
                "message": (
                    f"[stl] Generated {len(result.artifacts)} STL artifact(s); "
                    f"primary={result.stl_path.name} (faces={result.faces}, vertices={result.vertices})"
                ),
                "level": "info",
            })
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            for intent in stl_intents.values():
                if (
                    intent.subject_id == job.subject_id
                    and intent.preset == job.preset
                    and intent.params == job.params
                    and intent.status == "queued"
                ):
                    intent.status = "failed"
                    intent.updated_at = _utc_now()
                    intent.error = str(exc)
            await broadcast({
                "type": "log",
                "subject_id": job.subject_id,
                "stage": "stl",
                "message": f"[stl] Failed: {exc}",
                "level": "error",
            })
        finally:
            job.finished_at = _utc_now()
            stl_queue.task_done()
            await broadcast(_snapshot())


@app.on_event("startup")
async def _startup() -> None:
    global stl_worker_task
    restore_stats = _restore_checkpoint()
    print(
        "[startup] "
        f"DATA_DIR={DATA_DIR} OUTPUT_DIR={OUTPUT_DIR} "
        f"MRIQC_NPROCS={os.getenv('MRIQC_NPROCS', '1')} "
        f"MRIQC_OMP_NTHREADS={os.getenv('MRIQC_OMP_NTHREADS', '1')} "
        f"FASTSURFER_USE_GPU={os.getenv('FASTSURFER_USE_GPU', '0')} "
        f"FASTSURFER_GPU_DEVICE={os.getenv('FASTSURFER_GPU_DEVICE', 'all')} "
        f"FASTSURFER_DOCKER_USER={os.getenv('FASTSURFER_DOCKER_USER', '0:0')} "
        f"HOST_PROJECT_DIR(raw)={os.getenv('HOST_PROJECT_DIR', '')} "
        f"HOST_PROJECT_DIR(resolved:{runner.host_root_source})={runner.host_root}"
    )
    print(
        "[startup] "
        f"resume: restored_subjects={restore_stats['restored_subjects']} "
        f"active_subjects={restore_stats['active_subjects']} "
        f"interrupted_to_failed={restore_stats['interrupted']} "
        f"restored_logs={restore_stats['restored_logs']}"
    )
    if stl_worker_task is None or stl_worker_task.done():
        stl_worker_task = asyncio.create_task(_run_stl_worker())
    for sid in sorted(pipeline_state.subjects.keys()):
        await _process_stl_intents_for_subject(sid)


@app.on_event("shutdown")
async def _shutdown() -> None:
    global stl_worker_task
    interrupted = pipeline_state.mark_interrupted_running_as_failed()
    if interrupted:
        _append_interruption_logs(interrupted)
    _save_checkpoint_now()
    if stl_worker_task is not None:
        stl_worker_task.cancel()
        try:
            await stl_worker_task
        except asyncio.CancelledError:
            pass
        stl_worker_task = None


def _queue_stl_job(subject_id: str, payload: Dict[str, Any], *, skip_prereq: bool = False) -> STLJob:
    if not skip_prereq:
        prereq_error = _stl_prereq_error(subject_id)
        if prereq_error:
            raise HTTPException(status_code=409, detail=prereq_error)

    preset = str(payload.get("preset", "standard"))
    if preset not in STL_PRESETS:
        raise HTTPException(status_code=400, detail=f"Invalid preset '{preset}'")

    params = dict(payload.get("params", {}))
    job = STLJob(
        id=uuid.uuid4().hex,
        subject_id=subject_id,
        preset=preset,
        params=params,
        created_at=_utc_now(),
    )
    stl_jobs[job.id] = job
    stl_queue.put_nowait(job.id)
    return job


# -- WebSocket -----------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    connections.append(websocket)
    await websocket.send_text(json.dumps(_snapshot()))
    for msg in log_buffer[-200:]:
        try:
            await websocket.send_text(json.dumps(msg))
        except Exception:
            break
    try:
        while True:
            await websocket.receive_text()  # keep-alive
    except WebSocketDisconnect:
        if websocket in connections:
            connections.remove(websocket)


# -- Routes --------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/manual-mask", response_class=HTMLResponse)
async def manual_mask_page() -> HTMLResponse:
    html = (Path(__file__).parent / "static" / "manual_mask.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/subjects")
async def get_subjects() -> JSONResponse:
    _reload_subjects()
    return JSONResponse(_snapshot())


@app.post("/api/upload")
async def upload_bids(file: UploadFile) -> JSONResponse:
    if not (file.filename or "").endswith(".zip"):
        return JSONResponse({"error": "Upload a .zip of your BIDS dataset"}, status_code=400)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / "_upload.zip"
    tmp.write_bytes(await file.read())
    with zipfile.ZipFile(tmp, "r") as zf:
        zf.extractall(DATA_DIR)
    tmp.unlink(missing_ok=True)
    _reload_subjects()
    await broadcast(_snapshot())
    return JSONResponse({
        "message": "Uploaded and extracted",
        "subjects": list(pipeline_state.subjects.keys()),
    })


@app.post("/api/run/{subject_id}")
async def run_subject(subject_id: str) -> JSONResponse:
    _reload_subjects()
    if subject_id in pipeline_state.subjects:
        asyncio.create_task(_run(subject_id))
        return JSONResponse({"message": f"Started {subject_id}"})
    return JSONResponse({"error": "Subject not found"}, status_code=404)


@app.post("/api/run-all")
async def run_all() -> JSONResponse:
    _reload_subjects()
    for sid in list(pipeline_state.subjects.keys()):
        asyncio.create_task(_run(sid))
    return JSONResponse({"message": f"Started {len(pipeline_state.subjects)} subjects"})


@app.post("/api/run-stage/{subject_id}/{stage}")
async def run_subject_stage(subject_id: str, stage: str) -> JSONResponse:
    _reload_subjects()
    sub = pipeline_state.subjects.get(subject_id)
    if not sub:
        return JSONResponse({"error": "Subject not found"}, status_code=404)
    if stage not in STAGE_ORDER:
        return JSONResponse({"error": f"Unknown stage '{stage}'"}, status_code=400)
    if sub.overall_status == StageStatus.RUNNING:
        return JSONResponse({"error": f"{subject_id} is already running"}, status_code=409)
    if sub.stage_status.get(stage) == StageStatus.SKIPPED:
        requires = sorted(STAGE_REQUIRES.get(stage, set()))
        return JSONResponse(
            {"error": f"Stage '{stage}' is skipped for {subject_id} (missing modalities: {', '.join(requires)})"},
            status_code=409,
        )

    asyncio.create_task(_run(subject_id, stages=[stage]))
    return JSONResponse({"message": f"Started stage '{stage}' for {subject_id}"})


@app.get("/api/gate-config")
async def get_gate_config() -> JSONResponse:
    return JSONResponse({"gate_config": gate_config})


@app.post("/api/gate-config")
async def set_gate_config(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    stage = str(payload.get("stage", "")).strip()
    if stage not in STAGE_ORDER:
        return JSONResponse({"error": f"Unknown stage '{stage}'"}, status_code=400)
    current = gate_config.get(stage, {"mode": "auto", "trigger": "always"})
    mode = str(payload.get("mode", current.get("mode", "auto"))).strip().lower()
    trigger = str(payload.get("trigger", current.get("trigger", "always"))).strip().lower()
    if mode not in {"auto", "gated", "off"}:
        return JSONResponse({"error": "mode must be one of: auto, gated, off"}, status_code=400)
    if trigger not in {"always", "on_flag"}:
        return JSONResponse({"error": "trigger must be one of: always, on_flag"}, status_code=400)
    gate_config[stage] = {"mode": mode, "trigger": trigger}
    _save_checkpoint_now()
    await broadcast(_snapshot())
    return JSONResponse({"gate_config": gate_config})


@app.post("/api/gate/{subject_id}/{stage}")
async def decide_gate(subject_id: str, stage: str, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    _reload_subjects()
    sub = pipeline_state.subjects.get(subject_id)
    if not sub:
        return JSONResponse({"error": "Subject not found"}, status_code=404)
    if stage not in STAGE_ORDER:
        return JSONResponse({"error": f"Unknown stage '{stage}'"}, status_code=400)
    if stage != "mask":
        return JSONResponse({"error": f"Stage '{stage}' has no review gate"}, status_code=400)
    if sub.stage_status.get(stage) != StageStatus.AWAITING_REVIEW:
        return JSONResponse({"error": f"{subject_id}/{stage} is not awaiting review"}, status_code=409)

    decision = str(payload.get("decision", "")).strip().lower()
    note = str(payload.get("note", "")).strip()
    operator = str(payload.get("operator", "")).strip() or "operator"
    version_id = str(payload.get("version_id", "")).strip()

    if decision == "approve":
        if not version_id:
            versions = _list_mask_versions(subject_id)
            if not versions:
                return JSONResponse({"error": "No mask version available to approve"}, status_code=409)
            version_id = str(versions[0].get("version_id", "")).strip()
        job = _enqueue_stl_from_version(subject_id, version_id)
        pipeline_state.set_completed(subject_id, stage)
        mask_gate_details.pop(subject_id, None)
        _register_stage_artifacts(subject_id, stage)
        _record_gate_decision(subject_id, stage, "approve", version_id=version_id,
                              note=note, operator=operator, stl_job_id=(job.id if job else None))
        _save_checkpoint_now()
        await broadcast({"type": "log", "subject_id": subject_id, "stage": stage,
                         "message": f"gate approved (version={version_id}); STL queued, resuming pipeline",
                         "level": "stage"})
        await broadcast(_snapshot())
        asyncio.create_task(_run(subject_id))
        return JSONResponse({"message": "approved", "version_id": version_id,
                             "stl_job_id": (job.id if job else None)})

    if decision == "redo":
        try:
            saved = _build_mask_baseline(subject_id)
        except Exception as exc:
            return JSONResponse({"error": f"redo failed: {exc}"}, status_code=400)
        mask_gate_details.pop(subject_id, None)  # recompute QC for the new baseline
        _record_gate_decision(subject_id, stage, "redo", version_id=saved["version_id"],
                              note=note, operator=operator)
        _save_checkpoint_now()
        await broadcast({"type": "log", "subject_id": subject_id, "stage": stage,
                         "message": f"gate redo — new baseline {saved['version_id']}", "level": "info"})
        await broadcast(_snapshot())
        return JSONResponse({"message": "redone", "version": saved})

    if decision == "skip":
        sub.stage_status[stage] = StageStatus.SKIPPED
        mask_gate_details.pop(subject_id, None)
        _record_gate_decision(subject_id, stage, "skip", version_id=(version_id or None),
                              note=note, operator=operator)
        _save_checkpoint_now()
        await broadcast({"type": "log", "subject_id": subject_id, "stage": stage,
                         "message": "gate skipped; resuming pipeline", "level": "info"})
        await broadcast(_snapshot())
        asyncio.create_task(_run(subject_id))
        return JSONResponse({"message": "skipped"})

    return JSONResponse({"error": "decision must be one of: approve, redo, skip"}, status_code=400)


@app.post("/api/rerun/{subject_id}/{stage}")
async def rerun_from_stage(subject_id: str, stage: str, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """Reprocess a stage and everything downstream of it (reprocess cascade)."""
    _reload_subjects()
    sub = pipeline_state.subjects.get(subject_id)
    if not sub:
        return JSONResponse({"error": "Subject not found"}, status_code=404)
    if stage not in STAGE_ORDER:
        return JSONResponse({"error": f"Unknown stage '{stage}'"}, status_code=400)
    if sub.overall_status == StageStatus.RUNNING:
        return JSONResponse({"error": f"{subject_id} is already running"}, status_code=409)
    if any(st == StageStatus.AWAITING_REVIEW for st in sub.stage_status.values()):
        return JSONResponse({"error": "Resolve the open review gate before reprocessing"}, status_code=409)

    changed = pipeline_state.mark_for_rerun(subject_id, stage)
    if not changed:
        return JSONResponse({"error": f"Nothing to reprocess from '{stage}' (skipped or unknown)"}, status_code=409)

    _save_checkpoint_now()
    await broadcast({
        "type": "log", "subject_id": subject_id, "stage": stage,
        "message": f"[reprocess] re-running from {stage}: {', '.join(changed)}", "level": "stage",
    })
    await broadcast(_snapshot())
    if bool(payload.get("run", True)):
        asyncio.create_task(_run(subject_id))
    return JSONResponse({"message": "reprocess queued", "stages": changed})


@app.get("/api/group-stats")
async def list_group_stats() -> JSONResponse:
    out_dir = OUTPUT_DIR / "group"
    items: List[Dict[str, Any]] = []
    if out_dir.is_dir():
        for f in sorted(out_dir.glob("group_*.json"), reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                items.append({
                    "file": f.name, "kind": d.get("kind"), "comparison": d.get("comparison"),
                    "n_significant": d.get("n_significant"), "created_at": d.get("created_at"),
                })
            except Exception:
                continue
    return JSONResponse({"results": items})


@app.post("/api/group-stats")
async def run_group_stats(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """Two-group hypothesis test over per-subject artifacts (resolved by role)."""
    _reload_subjects()
    target = str(payload.get("target", "network")).strip().lower()
    try:
        alpha = float(payload.get("alpha", 0.05))
    except (TypeError, ValueError):
        return JSONResponse({"error": "alpha must be a number"}, status_code=400)

    groups = payload.get("groups")
    if not groups:
        column = str(payload.get("participants_column", "")).strip()
        if column:
            groups = groups_from_participants(DATA_DIR, column)
    if not isinstance(groups, dict) or len(groups) != 2:
        return JSONResponse(
            {"error": "Provide exactly two groups, or a participants_column with two values"},
            status_code=400,
        )

    # Covariates: inline {subject: {name: value}}, or read columns from participants.tsv.
    covariates = payload.get("covariates")
    cov_columns = payload.get("covariate_columns") or payload.get("covariates_columns")
    if not covariates and cov_columns:
        cols = cov_columns if isinstance(cov_columns, list) else [c.strip() for c in str(cov_columns).split(",")]
        covariates = covariates_from_participants(DATA_DIR, [c for c in cols if c])
    if not isinstance(covariates, dict):
        covariates = None

    manifest.load()
    try:
        if target in ("network", "network_metrics"):
            metrics_by_subject: Dict[str, Any] = {}
            for sids in groups.values():
                for sid in sids:
                    path = manifest.resolve_path(sid, "network_metrics")
                    if path and path.is_file():
                        try:
                            metrics_by_subject[sid] = json.loads(path.read_text(encoding="utf-8"))
                        except Exception:
                            pass
            result = compare_network_metrics(metrics_by_subject, groups, alpha=alpha, covariates=covariates)
        elif target in ("fc", "fc_matrix"):
            fc_by_subject: Dict[str, Any] = {}
            for sids in groups.values():
                for sid in sids:
                    path = manifest.resolve_path(sid, "fc_matrix")
                    if path and path.is_file():
                        try:
                            fc_by_subject[sid] = np.load(path)
                        except Exception:
                            pass
            method = str(payload.get("method", "permutation")).strip().lower()
            if method in ("screen", "fdr", "mass_univariate"):
                result = compare_fc_matrices(fc_by_subject, groups, alpha=alpha)
            elif method == "nbs":
                try:
                    n_perm = int(payload.get("n_perm", 1000))
                except (TypeError, ValueError):
                    n_perm = 1000
                try:
                    threshold = float(payload.get("nbs_threshold", 3.0))
                except (TypeError, ValueError):
                    threshold = 3.0
                result = compare_fc_nbs(fc_by_subject, groups, alpha=alpha, threshold=threshold, n_perm=n_perm)
            else:
                try:
                    n_perm = int(payload.get("n_perm", 5000))
                except (TypeError, ValueError):
                    n_perm = 5000
                result = compare_fc_permutation(fc_by_subject, groups, alpha=alpha, n_perm=n_perm, covariates=covariates)
        else:
            return JSONResponse({"error": "target must be 'network' or 'fc'"}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    out_dir = OUTPUT_DIR / "group"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    result["created_at"] = _utc_now()
    fname = f"group_{result['kind']}_{stamp}.json"
    (out_dir / fname).write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["saved_as"] = f"group/{fname}"

    await broadcast({
        "type": "log", "subject_id": "(group)", "stage": "group-stats",
        "message": f"[group] {result['comparison']} · {result['kind']} · "
                   f"{result['n_significant']} significant (alpha={alpha})",
        "level": "stage",
    })
    return JSONResponse(result)


async def _run_ingest(cmd: List[str], participant: str) -> None:
    """Stream a dcm2bids docker run, then refresh the subject list."""
    await broadcast({"type": "log", "subject_id": "(ingest)", "stage": "ingest",
                     "message": f"dcm2bids: {' '.join(cmd)}", "level": "stage"})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            await broadcast({"type": "log", "subject_id": "(ingest)", "stage": "ingest",
                             "message": raw.decode("utf-8", errors="replace").rstrip(), "level": "info"})
        await proc.wait()
        ok = proc.returncode == 0
    except FileNotFoundError:
        await broadcast({"type": "log", "subject_id": "(ingest)", "stage": "ingest",
                         "message": "'docker' not found — DICOM ingestion needs Docker.", "level": "error"})
        ok = False
    except Exception as exc:
        await broadcast({"type": "log", "subject_id": "(ingest)", "stage": "ingest",
                         "message": f"ingest failed: {exc}", "level": "error"})
        ok = False

    _reload_subjects()
    await broadcast({"type": "log", "subject_id": "(ingest)", "stage": "ingest",
                     "message": f"ingest {'complete' if ok else 'failed'} for {participant}",
                     "level": "stage" if ok else "error"})
    await broadcast(_snapshot())


@app.post("/api/ingest/dicom")
async def ingest_dicom(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """Convert a DICOM directory to BIDS (dcm2bids in Docker) into the data dir."""
    dicom_dir = str(payload.get("dicom_dir", "")).strip()
    participant = str(payload.get("participant", "")).strip()
    session = str(payload.get("session", "")).strip() or None
    if not dicom_dir or not participant:
        return JSONResponse({"error": "dicom_dir and participant are required"}, status_code=400)
    if not Path(dicom_dir).is_dir():
        return JSONResponse({"error": f"DICOM directory not found: {dicom_dir}"}, status_code=400)

    config = str(payload.get("config", "")).strip()
    if config:
        config_path = Path(config)
        if not config_path.is_file():
            return JSONResponse({"error": f"Config not found: {config}"}, status_code=400)
    else:
        config_path = DATA_DIR / ".dcm2bids_config.json"
        if not config_path.is_file():
            write_default_config(config_path)

    cmd = build_dcm2bids_command(
        dicom_dir=str(Path(dicom_dir)), participant=participant,
        output_dir=str(DATA_DIR), config=str(config_path), session=session,
    )
    asyncio.create_task(_run_ingest(cmd, participant))
    return JSONResponse({"message": f"DICOM ingestion started for {participant}",
                         "config": str(config_path)})


def _safe_label(participant: str) -> str:
    base = participant.strip()
    base = base[4:] if base.lower().startswith("sub-") else base
    return "".join(c for c in base if c.isalnum() or c in "-_")


@app.post("/api/ingest/dicom-upload")
async def ingest_dicom_upload(
    file: UploadFile,
    participant: str = Form(...),
    session: str = Form(""),
) -> JSONResponse:
    """Clinician front door: upload a DICOM .zip; convert to BIDS via dcm2bids."""
    if not (file.filename or "").lower().endswith(".zip"):
        return JSONResponse({"error": "Upload a .zip of the DICOM folder"}, status_code=400)
    label = _safe_label(participant)
    if not label:
        return JSONResponse({"error": "A valid subject id is required (e.g. sub-01)"}, status_code=400)

    # Extract under the host-mounted data dir so the dcm2bids container can mount it.
    ingest_dir = DATA_DIR / ".dicom_ingest" / label
    if ingest_dir.exists():
        shutil.rmtree(ingest_dir, ignore_errors=True)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f"_dicom_{label}.zip"
    tmp.write_bytes(await file.read())
    try:
        with zipfile.ZipFile(tmp, "r") as zf:
            zf.extractall(ingest_dir)
    except zipfile.BadZipFile:
        tmp.unlink(missing_ok=True)
        return JSONResponse({"error": "Not a valid .zip archive"}, status_code=400)
    finally:
        tmp.unlink(missing_ok=True)

    config_path = DATA_DIR / ".dcm2bids_config.json"
    if not config_path.is_file():
        write_default_config(config_path)

    cmd = build_dcm2bids_command(
        dicom_dir=runner._host_bind("data", ".dicom_ingest", label),
        participant=f"sub-{label}",
        output_dir=runner._host_bind("data"),
        config=runner._host_bind("data", ".dcm2bids_config.json"),
        session=(session.strip() or None),
    )
    asyncio.create_task(_run_ingest(cmd, f"sub-{label}"))
    return JSONResponse({"message": f"DICOM upload received for sub-{label}; converting…"})


@app.post("/api/reset")
async def reset_pipeline() -> JSONResponse:
    log_buffer.clear()
    pipeline_state.reset_all()
    stl_intents.clear()
    _save_checkpoint_now()
    await broadcast(_snapshot())
    return JSONResponse({"message": "Pipeline reset"})


@app.post("/api/stl/{subject_id}")
async def queue_stl_for_subject(subject_id: str, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    intent = _upsert_stl_intent(subject_id, payload)
    start_result = _start_fastsurfer_if_possible(subject_id)
    await _process_stl_intents_for_subject(subject_id)

    if intent.status == "queued":
        await broadcast({
            "type": "log",
            "subject_id": subject_id,
            "stage": "stl",
            "message": f"[stl] Intent queued immediately (preset={intent.preset})",
            "level": "stage",
        })
        return JSONResponse({
            "message": "STL job queued",
            "intent_key": intent.key,
            "deferred": False,
            "fastsurfer": start_result,
        })

    if start_result == "skipped":
        return JSONResponse(
            {"error": f"FastSurfer is skipped for {subject_id} (missing required modalities)."},
            status_code=409,
        )

    await broadcast({
        "type": "log",
        "subject_id": subject_id,
        "stage": "stl",
        "message": (
            f"[stl] Deferred intent stored (preset={intent.preset}); "
            f"fastsurfer={start_result}"
        ),
        "level": "stage",
    })
    await broadcast(_snapshot())
    return JSONResponse({
        "message": "STL intent deferred until FastSurfer completes",
        "intent_key": intent.key,
        "deferred": True,
        "fastsurfer": start_result,
    }, status_code=202)


@app.post("/api/stl-all")
async def queue_stl_for_all(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    _reload_subjects()
    queued: List[str] = []
    deferred: List[str] = []
    skipped: List[Dict[str, str]] = []
    for sid in sorted(pipeline_state.subjects.keys()):
        try:
            intent = _upsert_stl_intent(sid, payload)
        except HTTPException as exc:
            skipped.append({"subject_id": sid, "reason": str(exc.detail)})
            continue

        start_result = _start_fastsurfer_if_possible(sid)
        if start_result == "skipped":
            skipped.append({"subject_id": sid, "reason": "FastSurfer skipped (missing required modalities)"})
            continue

        await _process_stl_intents_for_subject(sid)
        if intent.status == "queued":
            queued.append(intent.key)
        else:
            deferred.append(intent.key)

    await broadcast(_snapshot())
    return JSONResponse({
        "message": f"Queued {len(queued)} STL jobs, deferred {len(deferred)} intents",
        "queued_intents": queued,
        "deferred_intents": deferred,
        "skipped": skipped,
    })


@app.post("/api/stl/cancel/{job_id}")
async def cancel_stl(job_id: str) -> JSONResponse:
    job = stl_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.status == "pending":
        job.status = "cancelled"
        job.finished_at = _utc_now()
        await broadcast(_snapshot())
        return JSONResponse({"message": "Job cancelled"})
    if job.status == "running":
        return JSONResponse({"error": "Running jobs cannot be force-cancelled yet"}, status_code=409)
    return JSONResponse({"message": f"Job already {job.status}"})


@app.get("/api/stl/jobs")
async def get_stl_jobs() -> JSONResponse:
    jobs = [asdict(j) for j in sorted(stl_jobs.values(), key=lambda x: x.created_at, reverse=True)]
    return JSONResponse({"jobs": jobs, "presets": sorted(STL_PRESETS.keys())})


@app.get("/api/stl/catalog/{subject_id}")
async def get_stl_catalog(subject_id: str) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    if not _find_fastsurfer_segmentation(subject_id):
        prereq_error = _stl_prereq_error(subject_id)
        if prereq_error:
            return JSONResponse({"error": prereq_error}, status_code=409)

    catalog = get_mask_catalog(subject_id, OUTPUT_DIR / "fastsurfer")
    return JSONResponse(catalog)


@app.get("/api/mask/catalog/{subject_id}")
async def get_mask_versions_catalog(subject_id: str) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)
    versions = _list_mask_versions(subject_id)
    return JSONResponse({
        "subject_id": subject_id,
        "versions": versions,
        "latest": versions[0] if versions else None,
    })


@app.post("/api/mask/init/{subject_id}")
async def init_mask_version(subject_id: str, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    parent_version_id = payload.get("parent_version_id")
    try:
        if isinstance(parent_version_id, str) and parent_version_id.strip():
            mask, img, parent_meta = _load_mask_version_array(subject_id, parent_version_id.strip())
            saved = _save_mask_version(
                subject_id=subject_id,
                mask=mask,
                reference_img=img,
                parent_version_id=parent_version_id.strip(),
                source_type="manual",
                operation_summary="init_from_parent",
                extra_meta={"source_version_id": parent_meta.get("version_id")},
            )
        else:
            source_type = str(payload.get("source_type", "auto")).strip().lower()
            if source_type in {"empty", "blank", "zero"}:
                mask, img, info = _build_empty_mask(subject_id)
                saved = _save_mask_version(
                    subject_id=subject_id,
                    mask=mask,
                    reference_img=img,
                    parent_version_id=None,
                    source_type="empty",
                    operation_summary="init_empty_mask",
                    extra_meta=info,
                )
            elif source_type in {"auto", "automatic", "selection"}:
                normalized = _normalize_stl_payload(payload)
                preset = normalized["preset"]
                params = normalized["params"]
                if preset in {"by_region", "by_lobe", "by_network", "by_tissue"}:
                    labels = _resolve_auto_labels(subject_id, preset, params)
                    if labels:
                        params["include_labels"] = labels
                mask, img, info = _build_auto_mask(subject_id, preset, params)
                saved = _save_mask_version(
                    subject_id=subject_id,
                    mask=mask,
                    reference_img=img,
                    parent_version_id=None,
                    source_type="auto",
                    operation_summary="init_from_automatic_selection",
                    extra_meta=info,
                )
            else:
                return JSONResponse({
                    "error": "Unsupported source_type. Use one of: auto, empty",
                }, status_code=400)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    _mask_cache_invalidate(subject_id)
    _save_checkpoint_now()
    return JSONResponse({"message": "Mask version initialized", "version": saved})


@app.get("/api/mask/version/{subject_id}/{version_id}")
async def get_mask_version(subject_id: str, version_id: str) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)
    try:
        mask, _img, meta = _load_mask_version_array(subject_id, version_id)
        rel = _to_output_relative(_mask_version_nifti_path(subject_id, version_id))
        return JSONResponse({
            "subject_id": subject_id,
            "version_id": version_id,
            "meta": meta,
            "shape": [int(v) for v in mask.shape],
            "voxel_count": int(np.count_nonzero(mask)),
            "relative_path": rel,
            "url": f"/artifacts/{rel}",
        })
    except FileNotFoundError:
        return JSONResponse({"error": "Mask version not found"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/mask/slice/{subject_id}/{version_id}")
async def get_mask_slice(subject_id: str, version_id: str, plane: str = "axial", index: Optional[int] = None) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)
    try:
        mask, _img, _meta = _load_mask_version_array(subject_id, version_id)
    except FileNotFoundError:
        return JSONResponse({"error": "Mask version not found"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        payload = _extract_mask_slice(mask, plane, index)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({
        "subject_id": subject_id,
        "version_id": version_id,
        **payload,
    })


@app.get("/api/mask/orthoview/{subject_id}/{version_id}")
async def get_mask_orthoview(
    subject_id: str,
    version_id: str,
    axial: Optional[int] = None,
    coronal: Optional[int] = None,
    sagittal: Optional[int] = None,
) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)
    try:
        mask, _img, _meta = _load_mask_version_array(subject_id, version_id)
    except FileNotFoundError:
        return JSONResponse({"error": "Mask version not found"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        axial_slice = _extract_mask_slice(mask, "axial", axial)
        coronal_slice = _extract_mask_slice(mask, "coronal", coronal)
        sagittal_slice = _extract_mask_slice(mask, "sagittal", sagittal)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({
        "subject_id": subject_id,
        "version_id": version_id,
        "shape": [int(v) for v in mask.shape],
        "axial": axial_slice,
        "coronal": coronal_slice,
        "sagittal": sagittal_slice,
    })


@app.get("/api/mask/anatomy/{subject_id}")
async def get_mask_anatomy_orthoview(
    subject_id: str,
    axial: Optional[int] = None,
    coronal: Optional[int] = None,
    sagittal: Optional[int] = None,
) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    try:
        volume = _load_reference_anatomy_volume(subject_id)
        axial_slice = _extract_intensity_slice(volume, "axial", axial)
        coronal_slice = _extract_intensity_slice(volume, "coronal", coronal)
        sagittal_slice = _extract_intensity_slice(volume, "sagittal", sagittal)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({
        "subject_id": subject_id,
        "shape": [int(v) for v in volume.shape],
        "axial": axial_slice,
        "coronal": coronal_slice,
        "sagittal": sagittal_slice,
    })


@app.post("/api/mask/version/{subject_id}")
async def save_mask_version(subject_id: str, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    parent_version_id = str(payload.get("parent_version_id", "")).strip()
    if not parent_version_id:
        return JSONResponse({"error": "parent_version_id is required"}, status_code=400)

    operation = payload.get("operation", {})
    operations = payload.get("operations")
    if operations is None:
        if not isinstance(operation, dict):
            return JSONResponse({"error": "operation must be an object"}, status_code=400)
        operations_list: List[Dict[str, Any]] = [operation]
    else:
        if not isinstance(operations, list) or not operations:
            return JSONResponse({"error": "operations must be a non-empty list"}, status_code=400)
        operations_list = []
        for op in operations:
            if not isinstance(op, dict):
                return JSONResponse({"error": "each operation must be an object"}, status_code=400)
            operations_list.append(op)

    expected_current = str(payload.get("expected_current_version_id", "")).strip()
    allow_branch = bool(payload.get("allow_branch", False))

    try:
        latest_version_id = _latest_mask_version_id(subject_id)
        if expected_current and latest_version_id and expected_current != latest_version_id:
            return JSONResponse({
                "error": "Mask version conflict: expected current version is stale.",
                "latest_version_id": latest_version_id,
                "expected_current_version_id": expected_current,
            }, status_code=409)

        if latest_version_id and parent_version_id != latest_version_id and not allow_branch:
            return JSONResponse({
                "error": "Mask version conflict: parent is not the latest version. Refresh or set allow_branch=true.",
                "latest_version_id": latest_version_id,
                "parent_version_id": parent_version_id,
            }, status_code=409)

        mask, img, _meta = _load_mask_version_array(subject_id, parent_version_id)
        next_mask = mask
        summaries: List[str] = []
        for op in operations_list:
            next_mask, summary = _apply_mask_operation(next_mask, op)
            summaries.append(summary)

        if len(summaries) == 1:
            operation_summary = summaries[0]
        else:
            first = operations_list[0]
            first_type = str(first.get("type", "batch")).strip().lower() or "batch"
            operation_summary = f"batch_{first_type} points={len(operations_list)}"

        saved = _save_mask_version(
            subject_id=subject_id,
            mask=next_mask,
            reference_img=img,
            parent_version_id=parent_version_id,
            source_type="manual",
            operation_summary=operation_summary,
            extra_meta={
                "operation": operation if operations is None else None,
                "operations": operations_list if operations is not None else None,
                "operation_count": len(operations_list),
            },
        )
    except FileNotFoundError:
        return JSONResponse({"error": "parent mask version not found"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    _mask_cache_invalidate(subject_id)
    _save_checkpoint_now()
    return JSONResponse({"message": "Mask version saved", "version": saved})


@app.delete("/api/mask/version/{subject_id}/{version_id}")
async def delete_mask_version(subject_id: str, version_id: str, force: bool = False) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    versions = _list_mask_versions(subject_id)
    if not versions:
        return JSONResponse({"error": "No mask versions found"}, status_code=404)

    target = next((v for v in versions if str(v.get("version_id", "")).strip() == version_id), None)
    if not target:
        return JSONResponse({"error": "Mask version not found"}, status_code=404)

    latest_version_id = str(versions[0].get("version_id", "")).strip()
    if len(versions) <= 1 and not force:
        return JSONResponse({
            "error": "Cannot delete the only mask version without force=true.",
            "latest_version_id": latest_version_id,
        }, status_code=409)

    if version_id == latest_version_id and not force:
        return JSONResponse({
            "error": "Cannot delete latest version without force=true.",
            "latest_version_id": latest_version_id,
        }, status_code=409)

    if not force:
        children = [
            str(v.get("version_id", "")).strip()
            for v in versions
            if str(v.get("parent_version_id", "")).strip() == version_id
        ]
        if children:
            return JSONResponse({
                "error": "Cannot delete a version that has child versions without force=true.",
                "child_version_ids": children,
                "latest_version_id": latest_version_id,
            }, status_code=409)

    nifti = _mask_version_nifti_path(subject_id, version_id)
    sidecar = _mask_version_sidecar_path(subject_id, version_id)
    removed: Dict[str, bool] = {
        "nifti": False,
        "sidecar": False,
    }
    if nifti.exists():
        nifti.unlink()
        removed["nifti"] = True
    if sidecar.exists():
        sidecar.unlink()
        removed["sidecar"] = True

    _mask_cache_invalidate(subject_id)
    _save_checkpoint_now()
    await broadcast({
        "type": "log",
        "subject_id": subject_id,
        "stage": "mask",
        "message": f"[mask] Deleted version {version_id} (force={str(force).lower()})",
        "level": "stage",
    })
    await broadcast(_snapshot())
    return JSONResponse({
        "message": "Mask version deleted",
        "subject_id": subject_id,
        "version_id": version_id,
        "removed": removed,
    })


@app.post("/api/stl/from-mask/{subject_id}/{version_id}")
async def queue_stl_from_manual_mask(subject_id: str, version_id: str, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    nifti = _mask_version_nifti_path(subject_id, version_id)
    if not nifti.exists():
        return JSONResponse({"error": "Mask version not found"}, status_code=404)

    preset = str(payload.get("preset", "standard"))
    if preset not in STL_PRESETS:
        return JSONResponse({"error": f"Invalid preset '{preset}'"}, status_code=400)

    params = dict(payload.get("params", {}))
    params["manual_mask_path"] = str(nifti)
    params["manual_mask_version_id"] = version_id
    params["mask_mode"] = "manual"

    try:
        job = _queue_stl_job(subject_id, {"preset": preset, "params": params}, skip_prereq=True)
    except HTTPException as exc:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    await broadcast({
        "type": "log",
        "subject_id": subject_id,
        "stage": "stl",
        "message": f"[stl] Manual mask STL queued (version={version_id}, preset={preset}, job={job.id[:8]})",
        "level": "stage",
    })
    await broadcast(_snapshot())
    return JSONResponse({
        "message": "Manual mask STL job queued",
        "job_id": job.id,
        "subject_id": subject_id,
        "version_id": version_id,
        "preset": preset,
    })


@app.delete("/api/stl/{subject_id}")
async def delete_stl_subject(subject_id: str) -> JSONResponse:
    _reload_subjects()
    if subject_id not in pipeline_state.subjects:
        return JSONResponse({"error": "Subject not found"}, status_code=404)

    file_counts = _remove_stl_subject_outputs(subject_id)
    runtime_counts = _purge_stl_runtime_state(subject_id)
    _save_checkpoint_now()
    await broadcast(_snapshot())
    return JSONResponse({
        "message": f"Deleted STL outputs for {subject_id}",
        "subject_id": subject_id,
        "removed_files": file_counts,
        "removed_runtime": runtime_counts,
        "notice": "This permanently removed the STL file(s) and their sidecar metadata.",
    })


@app.delete("/api/stl")
async def delete_all_stl_outputs() -> JSONResponse:
    _reload_subjects()
    file_counts = {"stl": 0, "json": 0}
    for subject_id in list(pipeline_state.subjects.keys()):
        counts = _remove_stl_subject_outputs(subject_id)
        file_counts["stl"] += counts["stl"]
        file_counts["json"] += counts["json"]

    runtime_counts = _purge_stl_runtime_state(None)
    _save_checkpoint_now()
    await broadcast(_snapshot())
    return JSONResponse({
        "message": "Deleted all STL outputs",
        "removed_files": file_counts,
        "removed_runtime": runtime_counts,
        "notice": "This permanently removed STL files and their sidecar metadata for all subjects.",
    })


@app.get("/artifacts/{artifact_path:path}")
async def get_artifact(artifact_path: str) -> FileResponse:
    candidate = (OUTPUT_DIR / artifact_path).resolve()
    root = OUTPUT_DIR.resolve()
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=400, detail="Invalid artifact path")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(candidate)


# -- Pipeline execution --------------------------------------------------------
def _gate_for(stage: str) -> Dict[str, str]:
    cfg = gate_config.get(stage) or {}
    return {
        "mode": str(cfg.get("mode", "auto")),
        "trigger": str(cfg.get("trigger", "always")),
    }


def _mask_baseline_is_flagged(subject_id: str, saved: Dict[str, Any]) -> bool:
    # on_flag = the mask validator is not happy (empty, or too many disconnected
    # components → likely noise). Falls back to the voxel count if validation fails.
    try:
        path = _mask_version_nifti_path(subject_id, saved["version_id"])
        result = validate_artifact("mask_version", path)
        return not result.ok
    except Exception:
        return int(saved.get("voxel_count", 0)) <= 0


def _record_gate_decision(
    subject_id: str,
    stage: str,
    decision: str,
    *,
    version_id: Optional[str] = None,
    note: str = "",
    operator: str = "operator",
    stl_job_id: Optional[str] = None,
) -> None:
    gate_audit.append({
        "subject_id": subject_id,
        "stage": stage,
        "decision": decision,
        "version_id": version_id or None,
        "note": note or None,
        "operator": operator or "operator",
        "stl_job_id": stl_job_id,
        "timestamp": _utc_now(),
    })
    if len(gate_audit) > MAX_LOG_BUFFER:
        del gate_audit[:-MAX_LOG_BUFFER]


def _build_mask_baseline(subject_id: str) -> Dict[str, Any]:
    """Build + persist the automatic baseline mask version (no STL). Returns the saved version dict."""
    mask, ref_img, info = _build_auto_mask(subject_id, "standard", {"mask_mode": "whole"})
    saved = _save_mask_version(
        subject_id=subject_id,
        mask=mask,
        reference_img=ref_img,
        parent_version_id=None,
        source_type="auto",
        operation_summary="pipeline_auto_mask:whole",
        extra_meta=info,
    )
    _mask_cache_invalidate(subject_id)
    return saved


def _enqueue_stl_from_version(subject_id: str, version_id: str, preset: str = "standard") -> Optional[STLJob]:
    nifti = _mask_version_nifti_path(subject_id, version_id)
    if not nifti.exists():
        return None
    params = {
        "manual_mask_path": str(nifti),
        "manual_mask_version_id": version_id,
        "mask_mode": "manual",
    }
    try:
        return _queue_stl_job(subject_id, {"preset": preset, "params": params}, skip_prereq=True)
    except HTTPException:
        return None


def _mask_gate_detail(subject_id: str) -> Dict[str, Any]:
    """Reviewer-facing QC summary for a pending mask gate (cached per subject)."""
    cached = mask_gate_details.get(subject_id)
    if cached:
        return cached
    versions = _list_mask_versions(subject_id)
    if not versions:
        return {}
    latest = versions[0]
    version_id = str(latest.get("version_id", ""))
    detail: Dict[str, Any] = {
        "version_id": version_id,
        "voxel_count": int(latest.get("voxel_count", 0)),
        "selection": latest.get("selection") or latest.get("mask_mode") or "whole brain",
        "url": latest.get("url"),
    }
    try:
        res = validate_artifact("mask_version", _mask_version_nifti_path(subject_id, version_id))
        detail["n_components"] = int(res.qc.get("n_components", 0))
        detail["n_cavities"] = res.qc.get("n_cavities")
        detail["genus"] = res.qc.get("genus")
        detail["ok"] = bool(res.ok)
        detail["messages"] = list(res.messages)
    except Exception:
        pass
    mask_gate_details[subject_id] = detail
    return detail


def _register_stage_artifacts(subject_id: str, stage: str) -> List[str]:
    """Record a completed stage's canonical artifacts, then validate them. Best-effort."""
    try:
        roles = register_stage_outputs(manifest, subject=subject_id, stage=stage, output_dir=OUTPUT_DIR)
    except Exception:
        return []
    try:
        validate_and_record(manifest, subject_id, roles)
    except Exception:
        pass
    return roles


async def _run_mask_stage(subject_id: str) -> str:
    """Run the masking stage with gate handling. Returns completed|paused|skipped|failed."""
    gate = _gate_for("mask")
    mode = gate["mode"]
    sub = pipeline_state.subjects.get(subject_id)
    if sub is None:
        return "failed"

    if mode == "off":
        sub.stage_status["mask"] = StageStatus.SKIPPED
        _save_checkpoint_now()
        await broadcast({"type": "log", "subject_id": subject_id, "stage": "mask",
                         "message": "-- MASK skipped (gate mode: off) --", "level": "info"})
        await broadcast(_snapshot())
        return "skipped"

    pipeline_state.set_running(subject_id, "mask")
    _save_checkpoint_now()
    await broadcast(_snapshot())
    await broadcast({"type": "log", "subject_id": subject_id, "stage": "mask",
                     "message": "-- MASK --", "level": "stage"})

    try:
        saved = _build_mask_baseline(subject_id)
    except Exception as exc:
        pipeline_state.set_failed(subject_id, "mask")
        _save_checkpoint_now()
        await broadcast({"type": "log", "subject_id": subject_id, "stage": "mask",
                         "message": f"baseline mask failed: {exc}", "level": "error"})
        await broadcast(_snapshot())
        return "failed"

    await broadcast({"type": "log", "subject_id": subject_id, "stage": "mask",
                     "message": f"baseline mask version {saved['version_id']} ({saved.get('voxel_count', 0)} voxels)",
                     "level": "info"})

    needs_review = mode == "gated" and (
        gate["trigger"] == "always"
        or (gate["trigger"] == "on_flag" and _mask_baseline_is_flagged(subject_id, saved))
    )
    if needs_review:
        sub.stage_status["mask"] = StageStatus.AWAITING_REVIEW
        _save_checkpoint_now()
        await broadcast({"type": "log", "subject_id": subject_id, "stage": "mask",
                         "message": "awaiting review — open the mask editor to approve, redo, or skip",
                         "level": "stage"})
        await broadcast(_snapshot())
        return "paused"

    # auto (or on_flag that passed): export STL from the baseline and complete.
    job = _enqueue_stl_from_version(subject_id, saved["version_id"])
    _record_gate_decision(subject_id, "mask", "auto_approved",
                          version_id=saved["version_id"], note="auto",
                          stl_job_id=(job.id if job else None))
    pipeline_state.set_completed(subject_id, "mask")
    _register_stage_artifacts(subject_id, "mask")
    _save_checkpoint_now()
    await broadcast(_snapshot())
    return "completed"


async def _run(subject_id: str, stages: Optional[List[str]] = None) -> None:
    sub = pipeline_state.subjects.get(subject_id)
    if not sub or sub.overall_status.value == "running":
        return
    # Don't advance a subject that is paused at a review gate — resolve it first.
    if any(st == StageStatus.AWAITING_REVIEW for st in sub.stage_status.values()):
        return

    stage_plan = runner.pending_stages(sub) if stages is None else [s for s in stages if s in STAGE_ORDER]
    if not stage_plan:
        return

    for stage in stage_plan:
        current = sub.stage_status.get(stage)
        if current == StageStatus.SKIPPED:
            await broadcast({
                "type": "log", "subject_id": subject_id, "stage": stage,
                "message": f"-- {stage.upper()} skipped (missing required modalities) --", "level": "error",
            })
            continue
        if current == StageStatus.COMPLETED:
            await broadcast({
                "type": "log", "subject_id": subject_id, "stage": stage,
                "message": f"-- {stage.upper()} already completed; skipping --", "level": "info",
            })
            continue

        # Masking is handled in-process so it can pause at a review gate and let
        # the manual editor refine the baseline before STL export / downstream.
        if stage == "mask":
            outcome = await _run_mask_stage(subject_id)
            if outcome in ("paused", "failed"):
                break
            continue

        pipeline_state.set_running(subject_id, stage)
        _save_checkpoint_now()
        await broadcast(_snapshot())
        await broadcast({
            "type": "log", "subject_id": subject_id, "stage": stage,
            "message": f"-- {stage.upper()} --", "level": "stage",
        })

        failed = False
        stage_progress[subject_id] = {"stage": stage, "nodes_done": 0, "updated_at": _utc_now()}
        try:
            async for line in runner.run_stage(subject_id, stage):
                await broadcast({
                    "type": "log", "subject_id": subject_id,
                    "stage": stage, "message": line, "level": "info",
                })
                cur = stage_progress.setdefault(subject_id, {"stage": stage, "nodes_done": 0})
                cur["stage"] = stage
                cur["updated_at"] = _utc_now()
                prog = parse_progress(stage, line)
                if prog:
                    if "percent" in prog:
                        cur["percent"] = prog["percent"]
                    if prog.get("event") == "finished":
                        cur["nodes_done"] = cur.get("nodes_done", 0) + 1
                    if "node" in prog:
                        cur["node"] = prog["node"]
                    if "phase" in prog:
                        cur["phase"] = prog["phase"]
                    await broadcast({
                        "type": "progress", "subject_id": subject_id,
                        "stage": stage, "progress": dict(cur),
                    })
        except Exception as exc:
            await broadcast({
                "type": "log", "subject_id": subject_id,
                "stage": stage, "message": str(exc), "level": "error",
            })
            failed = True

        if failed:
            stage_progress.pop(subject_id, None)
            pipeline_state.set_failed(subject_id, stage)
            _save_checkpoint_now()
            await broadcast(_snapshot())
            break

        valid, validation_error = runner.validate_stage_outputs(subject_id, stage)
        if not valid:
            stage_progress.pop(subject_id, None)
            pipeline_state.set_failed(subject_id, stage)
            _save_checkpoint_now()
            await broadcast({
                "type": "log", "subject_id": subject_id,
                "stage": stage, "message": validation_error, "level": "error",
            })
            await broadcast(_snapshot())
            break

        stage_progress.pop(subject_id, None)
        pipeline_state.set_completed(subject_id, stage)
        roles = _register_stage_artifacts(subject_id, stage)
        _save_checkpoint_now()
        await broadcast(_snapshot())
        if roles:
            await broadcast({
                "type": "log", "subject_id": subject_id, "stage": stage,
                "message": f"[manifest] registered: {', '.join(roles)}", "level": "info",
            })
        if stage == "fastsurfer":
            await _process_stl_intents_for_subject(subject_id)


# -- Entry point ---------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("src.web_server:app", host="0.0.0.0", port=8080, reload=False)
