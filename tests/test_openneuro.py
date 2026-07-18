"""Tests for the OpenNeuro fetch module.

The offline test always runs. The real S3 fetch runs only when RUN_OPENNEURO_NETWORK=1 (so CI /
normal runs stay offline and fast).
"""
import os

import pytest

from pipeline.openneuro import _norm_subjects, fetch_openneuro


def test_norm_subjects_adds_prefix_and_skips_blanks():
    assert _norm_subjects(["01", "sub-02", "  ", "3"]) == ["sub-01", "sub-02", "sub-3"]


@pytest.mark.skipif(os.environ.get("RUN_OPENNEURO_NETWORK") != "1",
                    reason="set RUN_OPENNEURO_NETWORK=1 to run the real OpenNeuro S3 fetch")
def test_fetch_metadata_real(tmp_path):
    summary = fetch_openneuro("ds004796", [], tmp_path, log=lambda *_: None)
    assert summary["files"] > 0
    assert (tmp_path / "participants.tsv").is_file()
    assert (tmp_path / "dataset_description.json").is_file()
