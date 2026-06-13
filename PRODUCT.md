# Product

## Register

product

## Users

Two audiences sharing one tool:

- **Neuroimaging researchers** — comfortable with BIDS, but want to stop wiring
  tools together by hand. Context: running multi-subject batches that take hours,
  then testing hypotheses on the results.
- **Clinicians doing research** — not CLI users. They have DICOMs, not BIDS, and
  expect a GUI that gets them from raw data to an interpretable result without a
  terminal.

The job to be done: take neuroimaging data through trusted, peer-reviewed
open-source pipelines and arrive at a defensible answer to a hypothesis — with
the ability to pause and verify quality at each step.

## Product Purpose

An open-source orchestrator that wraps community-standard neuroimaging tools
(MRIQC, FastSurfer, fMRIPrep, MRtrix3, Nilearn, BCT) into one monitored,
resumable, reviewable workflow — plus 3D mask/STL export. Success = a researcher
or clinician can configure a pipeline, understand its cost before running, watch
real progress, review quality at gates, and get group-level, reproducible
results, all without touching the command line.

Note: the wrapped tools are research-grade and peer-reviewed, **not clinically
certified**. The product is for research and hypothesis testing, not diagnosis.

## Brand Personality

Precise, calm, trustworthy. The voice is that of a careful colleague: plain,
exact, never hype. It should feel like a well-made scientific instrument — every
control has a purpose, nothing decorative competes with the data. Confidence
through restraint, not flash.

## Anti-references

- Generic AI-generated SaaS dashboards (the homogenous look impeccable detects).
- Purple-to-blue gradients, glassmorphism, glow/neon effects.
- All-monospace "hacker terminal" styling used as decoration rather than for data.
- Tiny (<14px) dense text and cramped, padding-starved panels.
- Color as the only signal for status (fails colorblind users in a clinical tool).

## Design Principles

1. **The data is the hero.** Chrome recedes; segmentations, matrices, logs, and
   metrics get the contrast and space. Decoration never competes with content.
2. **Honest about cost and state.** Always show what a choice will cost (time,
   disk, downloads) and what is happening right now (progress, heartbeat). Never
   leave the operator guessing whether something is working or hung.
3. **Verification is a first-class step.** Pausing to review quality is built in,
   not bolted on. Gates, audit trails, and the mask editor are core, not extras.
4. **Expert confidence, clinician accessibility.** Dense enough for power users,
   legible and guided enough for a clinician who never opens a terminal.
5. **Reproducible by construction.** Provenance, versioning, and exact
   configuration are surfaced, because this tool produces scientific results.

## Accessibility & Inclusion

WCAG 2.1 AA minimum. Honor `prefers-reduced-motion`. All status and data
encodings are colorblind-safe: color is always paired with an icon, shape, or
label, never used alone. Full keyboard operability for the run/review workflow.
