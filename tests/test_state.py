from pipeline.state import (
    STAGE_ORDER,
    StageStatus,
    SubjectState,
    PipelineState,
    downstream_stages,
)


def test_stage_order_contains_mask_before_network():
    assert "mask" in STAGE_ORDER
    assert STAGE_ORDER.index("mask") < STAGE_ORDER.index("network")


def test_full_subject_all_pending():
    s = SubjectState("sub-001", {"anat", "func", "dwi"})
    assert s.stage_status["mask"] == StageStatus.PENDING
    assert s.stage_status["fmriprep"] == StageStatus.PENDING


def test_anat_only_skips_func_stages_but_allows_mask():
    s = SubjectState("sub-002", {"anat"})
    assert s.stage_status["fastsurfer"] == StageStatus.PENDING
    assert s.stage_status["fmriprep"] == StageStatus.SKIPPED  # needs func
    assert s.stage_status["mask"] == StageStatus.PENDING       # needs anat (have it)


def test_dependency_skip_mask_when_segmentation_skipped():
    # No anat -> fastsurfer skipped -> mask skipped via STAGE_DEPENDS_ON.
    s = SubjectState("sub-003", {"func"})
    assert s.stage_status["fastsurfer"] == StageStatus.SKIPPED
    assert s.stage_status["mask"] == StageStatus.SKIPPED


def test_persistence_roundtrip():
    s = SubjectState("sub-001", {"anat", "func"})
    s.stage_status["mask"] = StageStatus.AWAITING_REVIEW
    restored = SubjectState.from_dict(s.to_dict())
    assert restored.stage_status["mask"] == StageStatus.AWAITING_REVIEW
    assert restored.modalities == {"anat", "func"}


def test_overall_and_current_reflect_awaiting_review():
    s = SubjectState("sub-001", {"anat", "func", "dwi"})
    s.stage_status["mask"] = StageStatus.AWAITING_REVIEW
    assert s.overall_status == StageStatus.AWAITING_REVIEW
    assert s.current_stage == "mask"


def test_downstream_graph():
    assert downstream_stages("fmriprep") == ["connectivity", "network"]
    assert downstream_stages("fastsurfer") == ["mask"]
    assert downstream_stages("connectivity") == ["network"]
    assert downstream_stages("network") == []


def test_mark_for_rerun_cascade_preserves_skipped():
    ps = PipelineState()
    ps.subjects["sub-001"] = SubjectState("sub-001", {"anat", "func", "dwi"})
    sub = ps.subjects["sub-001"]
    for stage in sub.stage_status:
        sub.stage_status[stage] = StageStatus.COMPLETED
    sub.stage_status["mrtrix3"] = StageStatus.SKIPPED

    changed = ps.mark_for_rerun("sub-001", "fmriprep")
    assert changed == ["fmriprep", "connectivity", "network"]
    assert sub.stage_status["mask"] == StageStatus.COMPLETED       # not downstream of fmriprep
    assert sub.stage_status["mrtrix3"] == StageStatus.SKIPPED      # preserved
    assert sub.stage_status["connectivity"] == StageStatus.PENDING
