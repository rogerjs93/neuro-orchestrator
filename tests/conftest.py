"""Shared test setup + fixtures.

Web tests import web_server, which reads DATA_DIR/OUTPUT_DIR/MOCK_MODE at import
time — so we point them at throwaway temp dirs before anything imports it.
"""
import os
import sys
import tempfile
from pathlib import Path

# Ensure src/ is importable even if the pythonpath ini isn't applied.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_TMP = tempfile.mkdtemp(prefix="no_pytest_")
os.environ.setdefault("DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("OUTPUT_DIR", os.path.join(_TMP, "outputs"))
os.environ.setdefault("MOCK_MODE", "1")
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)
os.makedirs(os.environ["OUTPUT_DIR"], exist_ok=True)

import numpy as np
import pytest


@pytest.fixture
def ball():
    """Return a function that draws a solid binary ball into a volume."""
    def _ball(shape, center, r):
        zz, yy, xx = np.ogrid[:shape[0], :shape[1], :shape[2]]
        return ((zz - center[0]) ** 2 + (yy - center[1]) ** 2 + (xx - center[2]) ** 2) <= r * r
    return _ball


@pytest.fixture
def save_nii(tmp_path):
    """Return a function that saves an array as a NIfTI and returns its Path."""
    import nibabel as nib

    def _save(arr, name="mask.nii.gz"):
        p = tmp_path / name
        nib.save(nib.Nifti1Image(np.asarray(arr).astype("uint8"), np.eye(4)), str(p))
        return p
    return _save
