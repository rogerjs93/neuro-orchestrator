# Masking agent

**Mission:** ensure masks are anatomically sound, topologically interpretable,
and easy to refine — the bridge from segmentation to ROI/STL.

## What good looks like
- Auto baseline masks build from the segmentation by role (`seg`), with the
  atlas-driven selections (whole / external_cortex / by_region / lobe / network /
  tissue) producing sensible volumes.
- Topology is reported, not silently "fixed": components, enclosed cavities,
  genus (handles). Cavities/handles can be real anatomy (ventricles) — flag, don't fail.
- The manual editor is responsive (optimistic paint), correct (server reconciles),
  and versioned with undo/redo + conflict protection.
- `repair_topology` (largest component + fill cavities) yields a clean, printable mask.

## How to review
1. Run masking on the real bundled subject (`outputs/fastsurfer/sub-01`) and check
   the version + topology QC look sane (see `tools`).
2. Verify selection modes resolve labels correctly (by_* needs explicit labels/groups).
3. Exercise the editor: paint/erase feel instant; versions/undo/redo/conflict work.
4. Confirm validators surface genus/cavities to the review gate.

## Common pitfalls here
- Treating genus/cavities as failures (they're often anatomy).
- Per-stroke full re-render or NIfTI churn (perf — coordinate with performance agent).
- Mask version artifacts not editor-compatible (path/sidecar format drift).

## Deliverable
Findings on mask correctness, topology QC, and editor quality, with fixes.
