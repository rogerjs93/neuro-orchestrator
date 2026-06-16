# Pipeline review agents

Each subfolder is a focused **expert reviewer** for one aspect of the
neuro-orchestrator pipeline. Spin one up to audit + polish its area; together
they cover the whole product end to end.

Each agent folder contains:
- `agent.md` — the human-readable brief: role, what "good" looks like, how to
  review, the checklist, common pitfalls.
- `agent.yaml` — machine-readable config: scope, the files/globs to inspect,
  the standards it holds to, the concrete checks, the tools/commands to run, and
  the acceptance criteria.

## Roster

| Agent | Owns |
|---|---|
| [ui](ui/agent.md) | Visual design, layout, the design language (DESIGN.md) |
| [ux](ux/agent.md) | Interaction flows, usability, accessibility, plain language |
| [masking](masking/agent.md) | Mask generation, topology QC, the manual editor |
| [stl](stl/agent.md) | 3D mesh export, watertight/printable geometry |
| [pipeline-tools](pipeline-tools/agent.md) | The wrapped neuro tools, versions, alternatives, reproducibility |
| [data-management](data-management/agent.md) | BIDS/derivatives, manifest, provenance, DICOM ingestion |
| [statistics-research](statistics-research/agent.md) | Group stats rigor, hypothesis testing, reproducibility |
| [clinical](clinical/agent.md) | Research-use safety, provenance, data privacy, disclaimers |
| [performance](performance/agent.md) | Responsiveness, payloads, lag |
| [testing-qa](testing-qa/agent.md) | Test coverage, validation, CI |

## YAML schema

```yaml
name:        # short id (matches folder)
title:       # human title
mission:     # one line — what this expert is responsible for
scope:
  owns:      # list — what is in this agent's remit
  not:       # list — explicitly out of scope (handled by another agent)
inspect:
  paths:     # key files this agent reads first
  globs:     # broader patterns to sweep
references:  # standards/docs it holds the work to (DESIGN.md, peer-reviewed refs, …)
checks:      # the polish checklist
  - id:      # short id
    check:   # the assertion to verify
    severity: high | medium | low
tools:       # commands to run (detect, pytest, …)
deliverable: # what the agent produces (a prioritized findings report)
done_when:   # acceptance criteria for "polished"
```

## How to use
Point a subagent at one folder: "Act as the agent described in
`agents/<name>/agent.md` + `agent.yaml`; review the project against its checks
and return a prioritized findings report." Run several in parallel for a full
sweep, then triage by severity.

These are review/polish personas — they audit and recommend (and may fix within
their scope); they do not redefine product direction (see `PRODUCT.md`).
