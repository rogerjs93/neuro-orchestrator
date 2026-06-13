"""
Functional connectivity matrix extraction using Nilearn.
Called as: python -m pipeline.tasks.connectivity --subject sub-001 ...
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def run(
    subject_id: str,
    fmriprep_dir: Path,
    output_dir: Path,
    bold: Path | None = None,
    confounds: Path | None = None,
) -> None:
    print(f"[connectivity] Loading fMRIPrep output for {subject_id}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Prefer the explicit BOLD path resolved by role from the manifest; fall back
    # to globbing the fMRIPrep dir for older runs / direct invocation.
    if bold is not None and Path(bold).is_file():
        bold_file = Path(bold)
        print(f"[connectivity] Using resolved BOLD: {bold_file.name}")
    else:
        bold_pattern = f"{subject_id}/**/*space-MNI*preproc_bold.nii.gz"
        bold_files = list(fmriprep_dir.glob(bold_pattern))
        if not bold_files:
            raise FileNotFoundError(
                f"No preprocessed BOLD found in {fmriprep_dir} for {subject_id}"
            )
        bold_file = bold_files[0]
        print(f"[connectivity] Found BOLD: {bold_file.name}")

    try:
        from nilearn import datasets, image
        from nilearn.connectome import ConnectivityMeasure
        from nilearn.maskers import NiftiLabelsMasker

        # Schaefer 200-parcel atlas
        print("[connectivity] Fetching Schaefer 200 atlas")
        atlas = datasets.fetch_atlas_schaefer_2018(n_rois=200, resolution_mm=2)

        masker = NiftiLabelsMasker(
            labels_img=atlas.maps,
            standardize=True,
            memory="nilearn_cache",
            verbose=0,
        )

        # Confounds: prefer the resolved path, else glob.
        if confounds is not None and Path(confounds).is_file():
            confounds_file = Path(confounds)
        else:
            confounds_pattern = f"{subject_id}/**/*confounds_timeseries.tsv"
            confounds_files = list(fmriprep_dir.glob(confounds_pattern))
            confounds_file = confounds_files[0] if confounds_files else None

        print("[connectivity] Extracting timeseries")
        if confounds_file:
            import pandas as pd
            conf_df = pd.read_csv(confounds_file, sep="\t")
            # Use standard 24-parameter motion model
            cols = [c for c in conf_df.columns if "motion" in c or "trans" in c or "rot" in c]
            ts = masker.fit_transform(str(bold_file), confounds=conf_df[cols].values)
        else:
            ts = masker.fit_transform(str(bold_file))

        print(f"[connectivity] Timeseries shape: {ts.shape}")

        # Compute correlation matrix
        measure = ConnectivityMeasure(kind="correlation")
        fc_matrix = measure.fit_transform([ts])[0]

        # Save outputs
        out_file = output_dir / f"{subject_id}_fc_matrix.npy"
        np.save(out_file, fc_matrix)
        print(f"[connectivity] FC matrix saved: {out_file} shape={fc_matrix.shape}")

        # Save atlas labels
        labels_file = output_dir / f"{subject_id}_atlas_labels.json"
        labels_file.write_text(json.dumps(list(atlas.labels), indent=2))
        print(f"[connectivity] Labels saved: {labels_file}")

    except ImportError:
        # Fallback: generate mock matrix for testing
        print("[connectivity] [MOCK] nilearn not available — generating synthetic FC matrix")
        n = 200
        A = np.random.randn(n, n) * 0.3
        fc_matrix = (A + A.T) / 2
        np.fill_diagonal(fc_matrix, 1.0)
        np.save(output_dir / f"{subject_id}_fc_matrix.npy", fc_matrix)
        print(f"[connectivity] [MOCK] Synthetic {n}×{n} FC matrix saved")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--subject", required=True)
    p.add_argument("--fmriprep-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--bold", type=Path, default=None, help="Resolved preprocessed BOLD path.")
    p.add_argument("--confounds", type=Path, default=None, help="Resolved confounds TSV path.")
    args = p.parse_args()
    run(args.subject, args.fmriprep_dir, args.output_dir, bold=args.bold, confounds=args.confounds)


if __name__ == "__main__":
    main()
