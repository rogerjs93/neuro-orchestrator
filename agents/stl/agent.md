# STL agent

**Mission:** exported meshes are watertight, manifold, and genuinely printable —
faithful to the mask and provenance-tracked.

## What good looks like
- Marching-cubes mesh → smoothing → decimation produces a clean surface at the
  chosen preset (fast_preview / standard / high_quality).
- `repair_mesh` makes the mesh watertight/manifold (best-effort, never fatal),
  and `mesh_topology` (watertight, genus, euler) is recorded in the STL sidecar.
- Quality presets behave as true presets; advanced params are explicit overrides.
- Repeated exports are versioned (timestamped), never silently overwritten.
- Atlas-driven by_* exports require explicit selection (no silent whole-brain fallback).

## How to review
1. Export an STL from the real subject and inspect the sidecar's `mesh_topology`
   (watertight true? genus/euler sane?) and vertex/face counts vs preset.
2. Confirm decimation fallbacks work when quadric backend is unavailable.
3. Check manual-mask STL provenance (`source_manual_mask`) is recorded.
4. Verify presets map to expected smoothing/decimation; advanced overrides apply.

## Common pitfalls here
- Non-watertight meshes that won't slice for printing.
- Empty mask after filtering (over-aggressive min-component/threshold).
- Sidecar missing topology/provenance.
- Decimation that destroys thin structures.

## Deliverable
Findings on mesh quality, printability, presets, and provenance, with fixes.
