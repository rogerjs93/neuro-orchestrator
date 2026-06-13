"""Pipeline state — subjects, per-stage status, overall status."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class StageStatus(Enum):
    PENDING         = "pending"
    RUNNING         = "running"
    AWAITING_REVIEW = "awaiting_review"   # paused at a blocking gate for operator review
    COMPLETED       = "completed"
    FAILED          = "failed"
    SKIPPED         = "skipped"   # subject lacks required modality (or a dependency was skipped)


# Ordered list of pipeline stages
STAGE_ORDER: List[str] = [
    "mriqc",
    "fastsurfer",
    "fmriprep",
    "mrtrix3",
    "connectivity",
    "mask",
    "network",
]

# Which BIDS modalities are required for each stage
STAGE_REQUIRES: Dict[str, Set[str]] = {
    "mriqc":        {"anat"},
    "fastsurfer":   {"anat"},
    "fmriprep":     {"anat", "func"},
    "mrtrix3":      {"dwi"},
    "connectivity": {"func"},
    "mask":         {"anat"},
    "network":      set(),   # runs if any upstream stage completed
}

# Stages that depend on another stage's output, not just on a modality.
# If the prerequisite stage is skipped, the dependent stage is skipped too.
STAGE_DEPENDS_ON: Dict[str, str] = {
    "mask": "fastsurfer",   # masking/STL builds on the segmentation
}


@dataclass
class SubjectState:
    id: str
    modalities: Set[str]
    stage_status: Dict[str, StageStatus] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # STAGE_ORDER lists prerequisites before dependents, so a stage's
        # dependency status is already resolved by the time we reach it.
        for stage in STAGE_ORDER:
            reqs = STAGE_REQUIRES[stage]
            dep = STAGE_DEPENDS_ON.get(stage)
            if reqs and not reqs.issubset(self.modalities):
                self.stage_status[stage] = StageStatus.SKIPPED
            elif dep and self.stage_status.get(dep) == StageStatus.SKIPPED:
                self.stage_status[stage] = StageStatus.SKIPPED
            else:
                self.stage_status[stage] = StageStatus.PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "modalities": sorted(self.modalities),
            "stages": {stage: status.value for stage, status in self.stage_status.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubjectState":
        subject_id = str(data.get("id", "")).strip()
        modalities = {str(m) for m in data.get("modalities", []) if str(m).strip()}
        subject = cls(id=subject_id, modalities=modalities)

        raw_stages = data.get("stages", {})
        if isinstance(raw_stages, dict):
            for stage, raw_status in raw_stages.items():
                if stage not in STAGE_ORDER:
                    continue
                try:
                    subject.stage_status[stage] = StageStatus(str(raw_status))
                except Exception:
                    continue
        return subject

    @property
    def overall_status(self) -> StageStatus:
        active = [s for s in self.stage_status.values() if s != StageStatus.SKIPPED]
        if not active:
            return StageStatus.SKIPPED
        if any(s == StageStatus.RUNNING         for s in active): return StageStatus.RUNNING
        if any(s == StageStatus.AWAITING_REVIEW for s in active): return StageStatus.AWAITING_REVIEW
        if any(s == StageStatus.FAILED          for s in active): return StageStatus.FAILED
        if all(s == StageStatus.COMPLETED       for s in active): return StageStatus.COMPLETED
        return StageStatus.PENDING

    @property
    def current_stage(self) -> Optional[str]:
        for stage in STAGE_ORDER:
            if self.stage_status.get(stage) in (StageStatus.RUNNING, StageStatus.AWAITING_REVIEW):
                return stage
        for stage in reversed(STAGE_ORDER):
            if self.stage_status.get(stage) == StageStatus.COMPLETED:
                return stage
        return None

    @property
    def progress(self) -> tuple[int, int]:
        """Returns (completed_count, total_runnable_count)."""
        runnable = [s for s in STAGE_ORDER if self.stage_status.get(s) != StageStatus.SKIPPED]
        done     = [s for s in runnable   if self.stage_status.get(s) == StageStatus.COMPLETED]
        return len(done), len(runnable)


class PipelineState:
    def __init__(self) -> None:
        self.subjects: Dict[str, SubjectState] = {}

    def add_subject(self, subject_id: str, modalities: Set[str]) -> None:
        self.subjects[subject_id] = SubjectState(id=subject_id, modalities=modalities)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subjects": {
                subject_id: subject.to_dict()
                for subject_id, subject in self.subjects.items()
            }
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineState":
        state = cls()
        raw_subjects = data.get("subjects", {})
        if not isinstance(raw_subjects, dict):
            return state

        for subject_id, subject_data in raw_subjects.items():
            if not isinstance(subject_data, dict):
                continue
            subject_data = dict(subject_data)
            subject_data.setdefault("id", subject_id)
            subject = SubjectState.from_dict(subject_data)
            if subject.id:
                state.subjects[subject.id] = subject
        return state

    def reconcile_with_scan(self, scanned_modalities: Dict[str, Set[str]]) -> None:
        # Drop subjects no longer present in the active dataset.
        stale_subjects = set(self.subjects) - set(scanned_modalities)
        for subject_id in stale_subjects:
            self.subjects.pop(subject_id, None)

        # Add new subjects and refresh modality-dependent skip flags.
        for subject_id, modalities in scanned_modalities.items():
            if subject_id not in self.subjects:
                self.subjects[subject_id] = SubjectState(id=subject_id, modalities=modalities)
                continue

            subject = self.subjects[subject_id]
            subject.modalities = set(modalities)
            for stage in STAGE_ORDER:
                required = STAGE_REQUIRES[stage]
                has_modalities = not required or required.issubset(subject.modalities)
                dep = STAGE_DEPENDS_ON.get(stage)
                dep_skipped = dep is not None and subject.stage_status.get(dep) == StageStatus.SKIPPED
                current = subject.stage_status.get(stage, StageStatus.PENDING)
                if not has_modalities or dep_skipped:
                    subject.stage_status[stage] = StageStatus.SKIPPED
                elif current == StageStatus.SKIPPED:
                    subject.stage_status[stage] = StageStatus.PENDING

    def mark_interrupted_running_as_failed(self) -> List[Tuple[str, str]]:
        interrupted: List[Tuple[str, str]] = []
        for subject_id, subject in self.subjects.items():
            for stage in STAGE_ORDER:
                if subject.stage_status.get(stage) == StageStatus.RUNNING:
                    subject.stage_status[stage] = StageStatus.FAILED
                    interrupted.append((subject_id, stage))
        return interrupted

    def set_running(self, subject_id: str, stage: str) -> None:
        if subject_id in self.subjects:
            self.subjects[subject_id].stage_status[stage] = StageStatus.RUNNING

    def set_completed(self, subject_id: str, stage: str) -> None:
        if subject_id in self.subjects:
            self.subjects[subject_id].stage_status[stage] = StageStatus.COMPLETED

    def set_failed(self, subject_id: str, stage: str) -> None:
        if subject_id in self.subjects:
            self.subjects[subject_id].stage_status[stage] = StageStatus.FAILED

    def reset_all(self) -> None:
        for sub in self.subjects.values():
            sub.__post_init__()

    def reset_subject(self, subject_id: str) -> None:
        if subject_id in self.subjects:
            self.subjects[subject_id].__post_init__()
