import json

import pytest

from pipeline.manifest import ArtifactManifest, ensure_dataset_description


def _write(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_register_and_resolve_by_role(tmp_path):
    m = ArtifactManifest(tmp_path / "derivatives")
    seg = _write(tmp_path / "fastsurfer" / "sub-001" / "aseg.mgz", b"seg-bytes")
    art = m.register(subject="sub-001", role="seg", path=seg, stage="fastsurfer", tool="FastSurfer")
    assert art.sha256
    resolved = m.resolve_path("sub-001", "seg")
    assert resolved.name == "aseg.mgz"
    assert resolved.is_file()


def test_unknown_role_rejected(tmp_path):
    m = ArtifactManifest(tmp_path / "derivatives")
    f = _write(tmp_path / "x.bin")
    with pytest.raises(ValueError):
        m.register(subject="s", role="not_a_role", path=f, stage="x")


def test_provenance_and_validation_roundtrip(tmp_path):
    root = tmp_path / "derivatives"
    m = ArtifactManifest(root)
    seg = _write(tmp_path / "seg.mgz", b"seg")
    m.register(subject="sub-001", role="seg", path=seg, stage="fastsurfer")
    mask = _write(tmp_path / "mask.nii.gz", b"mask")
    m.register(subject="sub-001", role="mask_version", path=mask, stage="mask",
               inputs=m.input_refs("sub-001", ["seg"]))

    m.set_validation("sub-001", "mask_version", valid=True, qc={"genus": 0})

    reloaded = ArtifactManifest(root)
    art = reloaded.resolve("sub-001", "mask_version")
    assert art.valid is True
    assert art.qc["genus"] == 0
    assert [e["role"] for e in art.inputs] == ["seg"]


def test_is_stale_when_upstream_changes(tmp_path):
    m = ArtifactManifest(tmp_path / "derivatives")
    seg = _write(tmp_path / "seg.mgz", b"v1")
    m.register(subject="sub-001", role="seg", path=seg, stage="fastsurfer")
    mask = _write(tmp_path / "mask.nii.gz", b"mask")
    m.register(subject="sub-001", role="mask_version", path=mask, stage="mask",
               inputs=m.input_refs("sub-001", ["seg"]))
    assert m.is_stale("sub-001", "mask_version") is False

    # Upstream segmentation changes -> mask becomes stale.
    seg.write_bytes(b"v2-different")
    m.register(subject="sub-001", role="seg", path=seg, stage="fastsurfer")
    assert m.is_stale("sub-001", "mask_version") is True


def test_dataset_description_is_bids(tmp_path):
    root = tmp_path / "derivatives"
    ensure_dataset_description(root, generated_by=[{"Name": "FastSurfer", "Version": "2.2"}])
    desc = json.loads((root / "dataset_description.json").read_text())
    assert desc["DatasetType"] == "derivative"
    assert "BIDSVersion" in desc
    assert desc["GeneratedBy"][0]["Name"] == "FastSurfer"
