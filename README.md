# neuro-orchestrator

TUI pipeline dashboard wrapping FSL, FastSurfer, fMRIPrep, MRtrix3, Nilearn, and BCT into a single monitored workflow. Built in Python ([Textual](https://github.com/Textualize/textual)) with a Rust/[tuie](https://github.com/jake-stewart/tuie) rewrite planned for v1.0.

> ⚠️ **Research use only.** The wrapped tools are peer-reviewed, community-standard
> *research* software — they are **not** clinically certified medical devices. This
> project is for neuroimaging research and hypothesis testing, **not for diagnosis
> or any clinical decision-making.**

```
┌─ neuro-orchestrator ──────────────────────────────────────────────────┐
│  Subjects           │  Logs: sub-001 · FastSurfer                     │
│  ─────────────────  │  ──────────────────────────────────────────     │
│  sub-001  ✓  done   │  14:23:01  ─── FASTSURFER ───                  │
│  sub-002  ⟳  fmri   │  14:23:02  Loading CNN model...                │
│  sub-003  ○  —      │  14:23:04  Coronal pass...                      │
│  sub-004  ✗  bet    │  14:23:11  Writing segmentation...              │
│                     │  14:23:12  ✓ fastsurfer complete                │
│  [Run all][Run]     │                                                  │
│  [Reset]            │                                                  │
├─────────────────────┴───────────────────────────────────────────────── │
│  q: quit   r: run selected   a: run all   c: clear log                 │
└──────────────────────────────────────────────────────────────────────── │
```

## Pipeline stages

| Stage | Tool | Requires | Notes |
|---|---|---|---|
| mriqc | nipreps/mriqc | anat | Image quality metrics |
| fastsurfer | deepmi/fastsurfer | anat | DL segmentation, ~1 min |
| fmriprep | nipreps/fmriprep | anat + func | Full preprocessing |
| mrtrix3 | mrtrix3/mrtrix3 | dwi | Tractography, 100k streamlines |
| connectivity | Nilearn (Python) | func | Schaefer 200-parcel FC matrices |
| network | BCT + NetworkX (Python) | — | Clustering, efficiency, hubs |

---

## Prerequisites

- Docker Desktop (or Docker Engine on Linux)
- VS Code with the **Dev Containers** extension

---

## Setup

### 1. Clone and open in VS Code

```bash
git clone <this-repo>
cd neuro-orchestrator
code .
```

VS Code will prompt **"Reopen in Container"** — click it. This builds the orchestrator image and drops you into the container.

### 2. FreeSurfer license (needed for FastSurfer + fMRIPrep)

Register for free at https://surfer.nmr.mgh.harvard.edu/registration.html then place the `license.txt` file at:

```
licenses/license.txt
```

### 3. Add your BIDS data

Place your dataset in the `data/` folder following BIDS format:

```
data/
├── dataset_description.json
├── sub-001/
│   ├── anat/sub-001_T1w.nii.gz
│   ├── func/sub-001_task-rest_bold.nii.gz
│   └── dwi/sub-001_dwi.nii.gz
├── sub-002/
│   └── ...
```

Subjects without a required modality will have that stage automatically skipped (e.g. no `dwi/` → MRtrix3 skipped).

### 4. Run the pipeline

Inside the devcontainer terminal:

```bash
python src/orchestrator.py
```

Or start everything with docker compose from outside VS Code:

```bash
docker compose up orchestrator
```

Keyboard shortcuts inside the TUI:

| Key | Action |
|---|---|
| `a` | Run all subjects |
| `r` | Run selected subject |
| `c` | Clear log |
| `q` | Quit |

---

## Testing without real data

Enable mock mode to simulate the full pipeline with fake log output — no BIDS data, no licenses, no Docker tool images needed:

```bash
MOCK_MODE=1 python src/orchestrator.py
```

To create mock subjects for testing:

```bash
for i in 001 002 003; do
  mkdir -p data/sub-$i/{anat,func,dwi}
  touch data/sub-$i/anat/sub-${i}_T1w.nii.gz
  touch data/sub-$i/func/sub-${i}_task-rest_bold.nii.gz
done
```

---

## Development & tests

Install the dev dependencies and run the suite from the project root:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite covers the pipeline state machine, artifact manifest/adapters,
validators, topology, group statistics, DICOM-ingest command building, and the
web API. Real-data integration tests run automatically when a bundled dataset is
present and skip cleanly otherwise. CI (GitHub Actions, `.github/workflows/ci.yml`)
runs `pytest` on every push and pull request.

---

## Configuration

Edit `config/pipeline.yaml` to control which stages run, output spaces, parallelism, and resource limits. Key options:

```yaml
pipeline:
  parallel_subjects: 2   # subjects running simultaneously
  n_cpus: 4
  mem_gb: 16

stages:
  fastsurfer:
    gpu: false           # set true if NVIDIA GPU available
  connectivity:
    atlas: schaefer_2018
    n_parcels: 200       # 100, 200, 400, 600, 800, 1000
```

For Docker runs, FastSurfer GPU acceleration is controlled by environment variables:

```bash
# default in docker-compose.yml is disabled for compatibility/safety
FASTSURFER_USE_GPU=0
FASTSURFER_GPU_DEVICE=all   # or a specific index like 0
FASTSURFER_DOCKER_USER=0:0  # overrides nested docker run -u for FastSurfer
```

MRIQC can be memory-heavy. If you see `synthstrip` killed with exit code `137`, keep
MRIQC thread counts low:

```bash
MRIQC_NPROCS=1
MRIQC_OMP_NTHREADS=1
```

FastSurfer has automatic fallback enabled in the runner: if a GPU launch fails, it retries
once on CPU.

FastSurfer nested Docker runs also pass an explicit user mapping. The default is `0:0`,
which avoids the image's default `nonroot` startup guard when launched from the
orchestrator container. Override `FASTSURFER_DOCKER_USER` if your Docker setup needs a
different UID:GID.

STL quality presets now apply as true presets by default: if Advanced controls are not
edited, only the selected preset is sent to the backend. Advanced parameters are treated
as explicit overrides only after you edit them.

Repeated STL runs are versioned (timestamped filenames) instead of overwriting prior STL
artifacts, so you can compare quality variants safely.

Automatic STL masking now includes atlas-driven modes:

- `by_region` (one STL per selected region label)
- `by_lobe` (group-based selection)
- `by_network` (heuristic DKT/aseg network grouping)
- `by_tissue` (GM/WM/CSF/subcortical grouping)

The backend exposes a catalog endpoint used by the UI picker:

- `GET /api/stl/catalog/{subject_id}`

This endpoint returns available labels from the subject segmentation plus precomputed
group maps for lobe/network/tissue selection. `by_*` modes now require explicit
selection and no longer fall back silently to whole-brain export.

Previous STL results can be removed from the UI using the cleanup control in the STL
results panel. Cleanup is permanent: it deletes the generated `.stl` files, their
sidecar `.json` metadata files, and the matching STL job/result entries from the live
snapshot. The UI lets you choose the scope each time, either the current subject or
all subjects.

### Manual mask editor

The web UI now includes a dedicated manual mask workflow that stays separate from the
automatic STL path.

How to use it:

1. Complete FastSurfer for a subject.
2. In the STL panel, pick your automatic mask selection (region/lobe/network/tissue).
3. Open the editor either from `MANUAL EDITOR` in the STL panel, or from `EDIT MASK`
  in STL results/completed STL jobs for post-automatic refinement.
4. In the editor, choose one baseline:
  - `INIT FROM AUTO` to start from the automatic selection.
  - `INIT EMPTY MASK` to start from a zero mask and draw from scratch.
5. Review slices with anatomy underlay (same reference image dimensions as the mask)
  and adjust `Underlay opacity` as needed.
6. Apply manual operations (`paint`, `erase`, `grow`, `shrink`, `fill_holes`,
  `keep_largest`) to create new child versions.
7. For continuous drawing, use `STROKE PAINT` or `STROKE ERASE`, then drag on a
  slice canvas. One completed stroke commits exactly one new mask version.
8. Click `EXPORT STL FROM VERSION` to queue STL generation from the active manual
  mask version.
9. Use `UNDO` / `REDO` (or `Ctrl+Z` / `Ctrl+Y`) to move through recent version
  navigation history in the editor.

Manual mask artifacts are versioned under:

```
outputs/masks/{subject_id}/versions/
```

Each version writes:

- `{version_id}.nii.gz` binary mask volume
- `{version_id}.json` sidecar metadata (parent, source type, operation summary,
  voxel count)

Conflict protection:

- Saving a new version requires a non-stale parent by default.
- If the active version is not latest, the API returns `409` and reports
  `latest_version_id`.
- The editor auto-refreshes to the latest version on conflict.
- Stroke commits can be replayed on the latest version after conflict recovery.

Batch operation support:

- `POST /api/mask/version/{subject_id}` accepts either:
  - `operation` (single operation), or
  - `operations` (batched operation list, used by stroke mode).

Version deletion:

- Endpoint: `DELETE /api/mask/version/{subject_id}/{version_id}`
- Safe delete blocks removal of latest, only, or parent versions with children.
- Use `?force=true` to override those safety guards.

Manual-mask STL endpoint:

- `POST /api/stl/from-mask/{subject_id}/{version_id}`

Generated STL sidecars include `source_manual_mask` for provenance.

Manual-mask smoke test script:

```bash
python scripts/manual_mask_smoke.py --base-url http://localhost:8080 --subject sub-001
```

If `--subject` is omitted, the script picks the first available subject from
`/api/subjects`. The smoke run verifies:

- STL catalog availability
- mask init
- anatomy orthoview fetch
- empty mask init
- version load + slice fetch
- batched version save (`operations` payload)
- STL queue from saved manual version

To force CPU mode:

```bash
FASTSURFER_USE_GPU=0 docker-compose up orchestrator
```

### UI skill workflow (impeccable)

This repo uses a minimal Node toolchain for UI workflow automation on static pages.

Install Node tooling once:

```bash
npm install
```

Install the UI skill scaffold:

```bash
npm run ui:skill:install
```

On Windows, the installer uses a local `unzip` shim (`unzip.cmd` ->
`scripts/unzip.cmd`) so `impeccable` can extract archives even when a system
`unzip` binary is not installed.

Equivalent direct command:

```bash
npx impeccable skills install
```

UX guardrails for UI redesign passes:

- Keep current workflows equivalent on both pages: `/` and `/manual-mask`.
- Preserve route contracts and backend API payload compatibility.
- Preserve existing interaction semantics (run/reset/stage actions, STL flow, mask edit/export, undo/redo).
- Verify no regressions in keyboard flow, state feedback, and responsive behavior.

### Resume after restart

The orchestrator now writes pipeline checkpoints to:

```
outputs/state/pipeline_state.json
```

What is persisted:

- Per-subject stage status (`pending`, `running`, `completed`, `failed`, `skipped`)
- Subject modality snapshot
- Recent pipeline logs (bounded buffer)

Resume behavior on startup:

- Saved progress is restored for both TUI and web UI modes
- Any stage that was `running` during shutdown is restored as `failed`
- Rerunning a subject resumes from failed/pending stages (completed stages are not rerun)

`Reset` clears in-memory state and overwrites the checkpoint with a fresh state.

---

## Outputs

```
outputs/
├── mriqc/          ← IQM reports per subject
├── fastsurfer/     ← segmentation volumes
├── fmriprep/       ← preprocessed BOLD, confounds
├── mrtrix3/        ← tractography .tck files
├── connectivity/   ← FC matrices (.npy) + atlas labels
└── network/        ← graph metrics per subject (.json)
```

---

## Architecture notes

The orchestrator container is lightweight (~500 MB). It holds the TUI logic and the Python analysis stages (Nilearn, BCT, NetworkX). FSL-dependent stages are called by spawning the official Docker images via the mounted Docker socket — no Docker-in-Docker, no privileged mode required.

The Python analysis stack (`connectivity`, `network`) runs directly inside the orchestrator, so it doesn't need a separate image pull.

The TUI layer is intentionally kept thin and separate from the pipeline logic, making a future Rust/[tuie](https://github.com/jake-stewart/tuie) rewrite straightforward.
