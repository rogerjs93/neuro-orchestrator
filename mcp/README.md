# neuro-orchestrator-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes the
[neuro-orchestrator](https://github.com/rogerjs93/neuro-orchestrator) neuroimaging pipeline as
tools, so an MCP client (Claude Desktop, etc.) can drive it conversationally: list subjects, run
stages, review masks, run group statistics, and export 3D STL models.

It is a **thin HTTP client** of a *running* orchestrator. It does not embed the neuroimaging
stack, so it is lightweight (only `mcp` + `httpx`).

> Research use only. The orchestrator wraps peer-reviewed, research-grade tools; it is not a
> medical device and not for diagnosis.

## Prerequisite: the orchestrator must be running

Start neuro-orchestrator first (from its repo root):

```bash
docker-compose up -d --build orchestrator      # serves http://localhost:8080
# or, for a light run without pulling large tool images:
MOCK_MODE=1 python run_server.py
```

## Install / run

With [uv](https://docs.astral.sh/uv/) (no clone needed):

```bash
uvx --from git+https://github.com/rogerjs93/neuro-orchestrator.git#subdirectory=mcp neuro-orchestrator-mcp
```

Or from a clone:

```bash
cd mcp
uv sync
uv run neuro-orchestrator-mcp
```

Set `NEURO_ORCHESTRATOR_URL` if the orchestrator is not at `http://localhost:8080`.

## Claude Desktop config

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "neuro-orchestrator": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/rogerjs93/neuro-orchestrator.git#subdirectory=mcp",
        "neuro-orchestrator-mcp"
      ],
      "env": { "NEURO_ORCHESTRATOR_URL": "http://localhost:8080" }
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `list_subjects` | Subjects + full pipeline status snapshot |
| `get_participant_columns` | Columns in participants.tsv (for grouping/covariates) |
| `get_mask_groups` | Grouping vocabularies (lobe/network/tissue) |
| `get_mask_catalog(subject)` | Anatomical label catalog for a subject |
| `get_stl_catalog(subject)` | STL files already generated |
| `list_stl_jobs` | STL job statuses |
| `run_subject(subject)` / `run_all` | Run the full pipeline |
| `run_stage(subject, stage)` | Run one stage (mriqc, fastsurfer, fmriprep, mrtrix3, connectivity, mask, network) |
| `rerun_from_stage(subject, stage)` | Rerun from a stage + downstream |
| `get_gate_config` / `set_gate_config` | Review-gate configuration |
| `review_mask_gate(subject, decision)` | Approve / redo / skip a paused mask gate |
| `list_group_stats` / `run_group_stats(...)` | Two-group stats (permutation FWE / NBS / FDR), covariates |
| `generate_stl(subject, preset, params)` | Queue an STL export (presets + `remesh` for clean surfaces) |
| `ingest_dicom(dicom_dir, participant)` | DICOM → BIDS via dcm2bids |

Destructive endpoints (reset, delete) are intentionally not exposed.

## Develop / test

```bash
cd mcp
uv sync
# with the orchestrator running in MOCK_MODE on :8080:
uv run pytest            # tests skip themselves if the orchestrator is unreachable
uv run mcp dev src/neuro_orchestrator_mcp/server.py   # MCP Inspector
```

## License

MIT.
