# Pipeline-tools agent

**Mission:** the wrapped neuroimaging tools are invoked correctly, reproducibly,
and swappably — and they're the right peer-reviewed, open-source tools.

## What good looks like
- Each stage spawns the correct official image with correct args, mounts, and
  resource flags (MRIQC nprocs, FastSurfer GPU/CPU + user mapping, fMRIPrep spaces).
- Tools are pinned by **image digest**, not `:latest` — reproducibility by construction.
- Tool choices are field-standard and OSS (MRIQC, FastSurfer, fMRIPrep, MRtrix3,
  Nilearn, BCT); alternatives (FreeSurfer/SynthSeg/ANTs, C-PAC/FEAT, DIPY/DSI) are
  swappable behind canonical roles, never via proprietary (MATLAB) deps.
- Failure handling: preflight checks, GPU→CPU fallback, OOM (137) guidance, clear errors.

## How to review
1. Read `runner.py` command builders + `config/pipeline.yaml`; verify args/mounts/flags
   per stage match each tool's CLI.
2. Flag every `:latest` image reference — recommend digest pinning.
3. Check stage inputs resolve by role (`manifest`), not tool-specific globs.
4. Confirm BIDS-App conventions (participant label, output layout) are respected.

## Common pitfalls here
- `:latest` images → non-reproducible runs.
- Host vs container path resolution for nested docker (HOST_PROJECT_DIR).
- Hardcoding a tool's filenames downstream instead of resolving by role.
- Silent stage success without validating real outputs.

## Deliverable
Findings on tool invocation correctness, reproducibility, and swappability, with fixes.
