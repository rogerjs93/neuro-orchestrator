from pipeline.manifest import ArtifactManifest
from pipeline.adapters import register_stage_outputs


def _write(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_register_stage_outputs_and_provenance(tmp_path):
    out = tmp_path / "outputs"
    _write(out / "fastsurfer" / "sub-001" / "mri" / "aseg.mgz", b"seg")
    _write(out / "connectivity" / "sub-001_fc_matrix.npy", b"fc")
    _write(out / "connectivity" / "sub-001_atlas_labels.json", b"[]")
    _write(out / "network" / "sub-001_network_metrics.json", b"{}")
    _write(out / "masks" / "sub-001" / "versions" / "v20260101T000000Z.nii.gz", b"m1")
    _write(out / "masks" / "sub-001" / "versions" / "v20260101T010000Z.nii.gz", b"m2")

    m = ArtifactManifest(out / "derivatives")
    assert register_stage_outputs(m, subject="sub-001", stage="fastsurfer", output_dir=out) == ["seg"]
    assert register_stage_outputs(m, subject="sub-001", stage="connectivity", output_dir=out) == ["fc_matrix", "atlas_labels"]
    assert register_stage_outputs(m, subject="sub-001", stage="network", output_dir=out) == ["network_metrics"]
    assert register_stage_outputs(m, subject="sub-001", stage="mask", output_dir=out) == ["mask_version"]

    # resolve by role, not filename
    assert m.resolve_path("sub-001", "seg").name == "aseg.mgz"
    # mask picks the latest version
    assert m.resolve_path("sub-001", "mask_version").name == "v20260101T010000Z.nii.gz"
    # provenance edges built automatically
    assert [e["role"] for e in m.resolve("sub-001", "network_metrics").inputs] == ["fc_matrix"]
    assert [e["role"] for e in m.resolve("sub-001", "mask_version").inputs] == ["seg"]


def test_missing_output_is_skipped_not_fatal(tmp_path):
    out = tmp_path / "outputs"
    m = ArtifactManifest(out / "derivatives")
    # No files on disk -> returns empty list, no exception.
    assert register_stage_outputs(m, subject="sub-001", stage="fastsurfer", output_dir=out) == []
