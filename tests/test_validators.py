import json

import numpy as np

from pipeline.validators import validate_artifact


def test_fc_matrix_good(tmp_path):
    p = tmp_path / "fc.npy"
    np.save(p, np.corrcoef(np.random.randn(20, 50)))
    res = validate_artifact("fc_matrix", p)
    assert res.ok
    assert res.qc["shape"] == [20, 20]


def test_fc_matrix_mostly_nan(tmp_path):
    p = tmp_path / "fc.npy"
    np.save(p, np.full((20, 20), np.nan))
    res = validate_artifact("fc_matrix", p)
    assert not res.ok


def test_json_empty_fails(tmp_path):
    p = tmp_path / "n.json"
    p.write_text("{}")
    assert validate_artifact("network_metrics", p).ok is False


def test_missing_file_fails(tmp_path):
    assert validate_artifact("seg", tmp_path / "nope.mgz").ok is False


def test_nonempty_fallback(tmp_path):
    p = tmp_path / "x.stl"
    p.write_bytes(b"solid")
    assert validate_artifact("stl", p).ok


def test_mask_empty_vs_solid(ball, save_nii):
    empty = save_nii(np.zeros((20, 20, 20)), "empty.nii.gz")
    assert validate_artifact("mask_version", empty).ok is False

    solid = save_nii(ball((30, 30, 30), (15, 15, 15), 8), "solid.nii.gz")
    res = validate_artifact("mask_version", solid)
    assert res.ok
    assert res.qc["n_components"] == 1
    assert res.qc["genus"] == 0
