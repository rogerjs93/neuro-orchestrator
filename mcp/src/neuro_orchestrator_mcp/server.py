"""FastMCP server exposing the neuro-orchestrator pipeline as MCP tools.

Each tool is a thin wrapper over a REST endpoint of a running orchestrator. Run:
    neuro-orchestrator-mcp            # stdio transport (for Claude Desktop etc.)
Set NEURO_ORCHESTRATOR_URL if the orchestrator is not at http://localhost:8080.
"""
from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .client import OrchestratorClient, OrchestratorError

mcp = FastMCP("neuro-orchestrator")

STAGES = ["mriqc", "fastsurfer", "fmriprep", "mrtrix3", "connectivity", "mask", "network"]
STL_PRESETS = [
    "standard", "high_quality", "fast_preview", "external_cortex",
    "by_region", "by_lobe", "by_network", "by_tissue", "noise_suppressed",
]

_client: OrchestratorClient | None = None


def _c() -> OrchestratorClient:
    global _client
    if _client is None:
        _client = OrchestratorClient()
    return _client


async def _safe(coro: Any) -> Any:
    """Await a client call, converting connection/HTTP errors into a readable result."""
    try:
        return await coro
    except OrchestratorError as e:
        return {"error": str(e)}


# --------------------------------------------------------------------- discovery

@mcp.tool()
async def list_subjects() -> Any:
    """List all subjects and the full pipeline status snapshot: each subject's per-stage status
    (mriqc, fastsurfer, fmriprep, mrtrix3, connectivity, mask, network), review gates, and live
    progress. Use this first to see what is available and what state each subject is in."""
    return await _safe(_c().get("/api/subjects"))


@mcp.tool()
async def get_participant_columns() -> Any:
    """List the column names in the dataset's participants.tsv (e.g. sex, age, group). Use these
    as `participants_column` or `covariate_columns` when running group statistics."""
    return await _safe(_c().get("/api/participants/columns"))


@mcp.tool()
async def get_mask_groups() -> Any:
    """Return the fixed grouping vocabularies (by lobe / network / tissue) used to pick masking
    targets for STL export."""
    return await _safe(_c().get("/api/mask/groups"))


@mcp.tool()
async def get_mask_catalog(subject_id: str) -> Any:
    """Return the anatomical label catalog for a subject's FastSurfer segmentation: the regions
    available to mask and export (id, name, hemisphere, lobe/network/tissue groups)."""
    return await _safe(_c().get(f"/api/mask/catalog/{subject_id}"))


@mcp.tool()
async def get_stl_catalog(subject_id: str) -> Any:
    """List the STL files already generated for a subject, with their preset, face/vertex counts,
    and source segmentation."""
    return await _safe(_c().get(f"/api/stl/catalog/{subject_id}"))


@mcp.tool()
async def list_stl_jobs() -> Any:
    """List current and recent STL generation jobs and their status (queued/running/done/failed)."""
    return await _safe(_c().get("/api/stl/jobs"))


# ---------------------------------------------------------------------- pipeline

@mcp.tool()
async def run_subject(subject_id: str) -> Any:
    """Run the full pipeline for one subject (all applicable stages, in order). Returns immediately;
    poll `list_subjects` for progress."""
    return await _safe(_c().post(f"/api/run/{subject_id}"))


@mcp.tool()
async def run_all() -> Any:
    """Run the full pipeline for every subject in the dataset. Returns immediately; poll
    `list_subjects` for progress."""
    return await _safe(_c().post("/api/run-all"))


@mcp.tool()
async def run_stage(subject_id: str, stage: str) -> Any:
    """Run a single pipeline stage for one subject. `stage` must be one of:
    mriqc, fastsurfer, fmriprep, mrtrix3, connectivity, mask, network."""
    if stage not in STAGES:
        return {"error": f"Unknown stage '{stage}'. Valid stages: {', '.join(STAGES)}"}
    return await _safe(_c().post(f"/api/run-stage/{subject_id}/{stage}"))


@mcp.tool()
async def rerun_from_stage(subject_id: str, stage: str) -> Any:
    """Rerun a subject from a given stage, invalidating and recomputing that stage and everything
    downstream that depends on it. `stage` is one of the pipeline stages (see run_stage)."""
    if stage not in STAGES:
        return {"error": f"Unknown stage '{stage}'. Valid stages: {', '.join(STAGES)}"}
    return await _safe(_c().post(f"/api/rerun/{subject_id}/{stage}"))


# ------------------------------------------------------------------- review gate

@mcp.tool()
async def get_gate_config() -> Any:
    """Get the current review-gate configuration (per-stage mode auto/gated/off and trigger)."""
    return await _safe(_c().get("/api/gate-config"))


@mcp.tool()
async def set_gate_config(config: dict) -> Any:
    """Set the review-gate configuration. `config` maps a stage (currently only 'mask') to
    {"mode": "auto|gated|off", "trigger": "always|on_flag"}."""
    return await _safe(_c().post("/api/gate-config", json=config))


@mcp.tool()
async def review_mask_gate(subject_id: str, decision: str, note: str = "") -> Any:
    """Resolve a paused mask review gate for a subject. `decision` is one of:
    'approve' (accept the mask and export), 'redo' (rebuild the baseline mask), or 'skip'.
    Only the 'mask' stage has a review gate."""
    if decision not in ("approve", "redo", "skip"):
        return {"error": "decision must be one of: approve, redo, skip"}
    return await _safe(_c().post(f"/api/gate/{subject_id}/mask", json={"decision": decision, "note": note}))


# ---------------------------------------------------------------------- analysis

@mcp.tool()
async def list_group_stats() -> Any:
    """List previously computed group-statistics results saved for the dataset."""
    return await _safe(_c().get("/api/group-stats"))


@mcp.tool()
async def run_group_stats(
    target: str = "network",
    participants_column: Optional[str] = None,
    groups: Optional[dict] = None,
    fc_method: Optional[str] = None,
    covariate_columns: Optional[list[str]] = None,
    alpha: float = 0.05,
) -> Any:
    """Run a two-group statistical comparison (peer-reviewed methods).

    - target: 'network' (graph/network metrics) or 'fc' (functional-connectivity matrices).
    - Define the two groups EITHER by `participants_column` (a participants.tsv column with exactly
      two values) OR by `groups` = {"labelA": ["sub-01", ...], "labelB": [...]}.
    - fc_method (target='fc' only): 'permutation' (FWE, default), 'nbs', or 'screen' (FDR).
    - covariate_columns: participants.tsv numeric columns to adjust for (e.g. ["age"]).
    - alpha: significance threshold (default 0.05).
    """
    if target not in ("network", "fc"):
        return {"error": "target must be 'network' or 'fc'"}
    payload: dict[str, Any] = {"target": target, "alpha": alpha}
    if groups:
        payload["groups"] = groups
    elif participants_column:
        payload["participants_column"] = participants_column
    else:
        return {"error": "Provide participants_column or groups (two groups)."}
    if fc_method:
        payload["fc_method"] = fc_method
    if covariate_columns:
        payload["covariate_columns"] = covariate_columns
    return await _safe(_c().post("/api/group-stats", json=payload))


# ------------------------------------------------------------------------ export

@mcp.tool()
async def generate_stl(subject_id: str, preset: str = "standard", params: Optional[dict] = None) -> Any:
    """Queue a 3D STL export of a subject's brain from its FastSurfer segmentation.

    - preset: one of standard, high_quality, fast_preview, external_cortex, by_region, by_lobe,
      by_network, by_tissue, noise_suppressed.
    - params (optional overrides): e.g. {"selected_labels": [17, 53], "decimation_ratio": 0.6,
      "remesh": true, "remesh_ratio": 0.6}. `remesh` runs a uniform ACVD retopology for a cleaner,
      print-ready surface. Region/lobe/network/tissue presets require selected_labels/selected_groups.
    Returns a job; poll `list_stl_jobs` and read results with `get_stl_catalog`.
    """
    if preset not in STL_PRESETS:
        return {"error": f"Unknown preset '{preset}'. Valid presets: {', '.join(STL_PRESETS)}"}
    body: dict[str, Any] = {"preset": preset}
    if params:
        body["params"] = params
    return await _safe(_c().post(f"/api/stl/{subject_id}", json=body))


# --------------------------------------------------------------------- ingestion

@mcp.tool()
async def ingest_dicom(
    dicom_dir: str,
    participant: str,
    session: Optional[str] = None,
    config: Optional[str] = None,
) -> Any:
    """Convert a directory of DICOM files into a BIDS subject using dcm2bids (runs in Docker).

    - dicom_dir: path, on the orchestrator host, to the raw DICOM directory.
    - participant: BIDS participant label (with or without the 'sub-' prefix), e.g. '03'.
    - session: optional BIDS session label.
    - config: optional path to a dcm2bids config file (a sensible default is written if omitted).
    """
    payload: dict[str, Any] = {"dicom_dir": dicom_dir, "participant": participant}
    if session:
        payload["session"] = session
    if config:
        payload["config"] = config
    return await _safe(_c().post("/api/ingest/dicom", json=payload))


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
