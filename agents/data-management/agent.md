# Data-management agent

**Mission:** every artifact is discoverable by role, provenance-tracked, validated,
and stored to a reproducible BIDS-Derivatives layout — and DICOM ingestion is sound.

## What good looks like
- Canonical roles decouple downstream code from tool filenames; the manifest is the
  source of truth (role, path, tool+digest, params, sha256, input-hash provenance, QC, valid).
- Provenance graph is correct (e.g. mask_version←seg, network_metrics←fc_matrix);
  `is_stale` detects upstream changes for the reprocess cascade.
- BIDS-Derivatives: `dataset_description.json` with GeneratedBy; outputs migrating to
  `outputs/derivatives/` with proper entities.
- Validators record real QC (not just existence) into the manifest.
- DICOM→BIDS (dcm2bids) writes a valid BIDS tree the scanner picks up; messy
  `participants.tsv` handled gracefully.

## How to review
1. Read `manifest.py`, `adapters.py`, `validators.py`, `ingest.py`, `persistence.py`.
2. Verify adapters register every stage's role with correct provenance edges.
3. Check resolve-by-role is used at all consumer sites (no lingering tool-filename globs).
4. Confirm checkpoint/resume + gate audit trail persist and restore correctly.
5. Sanity-check the BIDS-Derivatives descriptor + layout plan.

## Common pitfalls here
- Consumers still globbing tool filenames instead of `resolve(role)`.
- Outputs not under a BIDS-Derivatives layout; missing provenance/digests.
- Messy phenotype data (e.g. `sex="M,"`) breaking grouping.

## Deliverable
Findings on artifact tracking, provenance, validation, layout, and ingestion, with fixes.
