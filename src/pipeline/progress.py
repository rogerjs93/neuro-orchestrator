"""
Best-effort progress parsing from tool log lines.

Turns raw stdout from the wrapped tools into structured progress so the UI can
show real movement — percent, current phase, or node counts — instead of a blind
spinner. Never raises and never blocks: an unparseable line returns None and the
stage still streams its raw log.

Recognises:
  - explicit percentages (MRtrix3 progress bars, etc.)            -> {"percent": N}
  - Nipype node lines (MRIQC / fMRIPrep / FastSurfer wrappers)    -> {"event","node"}
  - FastSurfer view passes                                        -> {"phase"}
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_PCT = re.compile(r"(?<![\d.])(\d{1,3})\s?%")
_NIPYPE_NODE = re.compile(r"\[\s*Node\s*\]\s+(Finished|Running|Setting-up)\s+\"?([\w.\-/]+)", re.I)
_FS_PASS = re.compile(r"\b(coronal|axial|sagittal)\b", re.I)


def parse_progress(stage: str, line: str) -> Optional[Dict[str, Any]]:
    if not line:
        return None
    s = line.strip()

    # Nipype node lifecycle (most informative for mriqc/fmriprep).
    m = _NIPYPE_NODE.search(s)
    if m:
        return {"event": m.group(1).lower(), "node": m.group(2)}

    # Explicit percentage (MRtrix3 tckgen etc.).
    m = _PCT.search(s)
    if m:
        pct = int(m.group(1))
        if 0 <= pct <= 100:
            return {"percent": pct}

    # FastSurfer view passes.
    if stage == "fastsurfer":
        m = _FS_PASS.search(s)
        if m:
            return {"phase": f"{m.group(1).lower()} pass"}

    return None
