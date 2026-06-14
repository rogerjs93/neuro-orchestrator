"""Integration tests against the bundled real dataset (OpenNeuro ds000001 +
real FastSurfer output). Skipped automatically when the data isn't present, so
CI without the data still passes.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEG_DIR = ROOT / "outputs" / "fastsurfer"
DATA_DIR = ROOT / "data"
HAS_SEG = (SEG_DIR / "sub-01").is_dir()
HAS_PARTICIPANTS = (DATA_DIR / "participants.tsv").is_file()


@pytest.fixture(scope="module")
def real_mask_out(tmp_path_factory):
    from pipeline.tasks import mask_stl
    out = tmp_path_factory.mktemp("real_out")
    mask_stl.run(
        subject_id="sub-01", fastsurfer_dir=SEG_DIR, output_dir=out,
        selection="whole", export_stl=True, stl_preset="fast_preview",
    )
    return out


@pytest.mark.skipif(not HAS_SEG, reason="bundled FastSurfer segmentation not present")
def test_masking_produces_mask_and_stl_on_real_data(real_mask_out):
    versions = list((real_mask_out / "masks" / "sub-01" / "versions").glob("*.nii.gz"))
    stls = list((real_mask_out / "stl" / "sub-01").glob("*.stl"))
    assert versions, "a mask version was written"
    assert stls, "an STL was exported"


@pytest.mark.skipif(not HAS_SEG, reason="bundled FastSurfer segmentation not present")
def test_topology_qc_on_real_mask(real_mask_out):
    from pipeline.validators import validate_artifact
    v = sorted((real_mask_out / "masks" / "sub-01" / "versions").glob("*.nii.gz"))[-1]
    res = validate_artifact("mask_version", v)
    assert res.ok
    assert res.qc["voxel_count"] > 100000          # a real whole brain
    assert res.qc["n_components"] == 1
    assert res.qc["genus"] is not None             # topology computed on real data


@pytest.mark.skipif(not HAS_SEG, reason="bundled FastSurfer segmentation not present")
def test_real_stl_mesh_is_watertight(real_mask_out):
    import json
    sidecar = sorted((real_mask_out / "stl" / "sub-01").glob("*.json"))[-1]
    meta = json.loads(sidecar.read_text())
    assert meta.get("mesh_topology", {}).get("watertight") is True


@pytest.mark.skipif(not HAS_SEG, reason="bundled FastSurfer segmentation not present")
def test_adapter_registers_real_seg(real_mask_out, tmp_path):
    # The segmentation adapter should resolve the real FastSurfer output by role.
    from pipeline.manifest import ArtifactManifest
    from pipeline.adapters import register_stage_outputs
    m = ArtifactManifest(tmp_path / "derivatives")
    roles = register_stage_outputs(m, subject="sub-01", stage="fastsurfer", output_dir=ROOT / "outputs")
    assert "seg" in roles
    assert m.resolve_path("sub-01", "seg").is_file()


@pytest.mark.skipif(not HAS_PARTICIPANTS, reason="bundled participants.tsv not present")
def test_groups_and_covariates_from_real_participants():
    from pipeline.group_stats import groups_from_participants, covariates_from_participants
    groups = groups_from_participants(DATA_DIR, "sex")
    assert "F" in groups and "M" in groups
    assert "sub-01" in groups["F"]
    covs = covariates_from_participants(DATA_DIR, ["age"])
    assert covs.get("sub-01", {}).get("age") == 26.0
