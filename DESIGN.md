# Design

Visual system for neuro-orchestrator. Direction: **refined scientific
instrument** — the craft of a modern dev tool with the restraint of a medical
workstation. Defaults below are a starting point; refine as the UI evolves.
Re-run `impeccable document` to recapture tokens once the redesign lands.

## Theme

Calm, low-noise, content-forward. Dark and light both first-class (researchers
run long sessions in dark; clinics often default to light). Flat surfaces, no
gradients/glow/shadows beyond functional focus rings. Chrome is near-neutral so
segmentations, matrices, and logs carry the color.

## Color

Neutral slate base + one deliberate accent. Avoid the purple→blue gradient tell.

- Base (dark):   bg `#0E1417`, surface `#161D21`, raised `#1E272C`, border `#2A353B`
- Base (light):  bg `#FBFCFC`, surface `#FFFFFF`, raised `#F3F6F7`, border `#D7DEE1`
- Text (dark):   primary `#E8EEF0`, secondary `#9DAAB0`, tertiary `#6B777C`
- Text (light):  primary `#10181C`, secondary `#4C595F`, tertiary `#79868C`
- Accent (instrument teal): `#1E9E8A` (light-mode text-safe `#0F6E60`), used
  sparingly for the single primary action and active state — not everywhere.

Status palette — colorblind-safe, **always paired with an icon/label**, never
color alone:

- pending `#6B777C` ○ · running `#3B82C4` ⟳ · awaiting-review `#C98A1A` ⏸
- completed `#1E9E8A` ✓ · failed `#C2483F` ✗ · skipped `#6B777C` —

Data viz (connectivity matrices, metrics): use a perceptually-uniform,
colorblind-safe scale (e.g. cividis/viridis), not a rainbow.

## Typography

A pairing, not a monolith — this is the single biggest move away from the
generic all-mono look.

- **UI / body / headings:** IBM Plex Sans. Weights 400 / 500 only (no 600/700).
  Scientific-engineering provenance, strong at small sizes, not overused like Inter.
- **Logs / IDs / metrics / code / file paths:** IBM Plex Mono. Keeps the
  terminal heritage where it's *functional* (streaming logs, voxel counts,
  version ids), not as decoration.
- Sizes: body 14px (min), secondary 13px, labels 12px. **No text below 12px**,
  and ≥14px for anything read as content (fixes the detector's tiny-text hits).
- Headings: h1 22 / h2 18 / h3 16, weight 500, sentence case always.
- Letter-spacing: default (0) on body. Wide tracking only on short uppercase
  labels (fixes the wide-tracking hit).

## Spacing & Layout

- 4px base scale: 4 / 8 / 12 / 16 / 24 / 32.
- **Insets ≥12px (ideally 16px) inside any bordered, outlined, or filled
  container** — fixes the cramped-padding hits; content never sits flush to an edge.
- Three-pane app shell: subjects/run list · main (stepper + progress + logs) ·
  contextual review/STL panel. Generous gutters; let panels breathe.
- Radius: 6px controls, 10px cards. Full borders only when rounded (no rounded
  single-side accents). Borders 1px `border` token, hairline feel.
- Popovers/modals (review gate, pickers) must escape clipping — no
  `overflow:hidden` ancestor traps them (fixes the clipped-overflow hits).

## Components

- **Stage stepper:** horizontal chips, status = color + icon + short label; the
  active/awaiting chip gets a 2px accent ring (the only 2px border in the system).
- **Progress block:** node/percent + elapsed + ETA + a heartbeat ("last line Ns
  ago"). Mono for the numbers.
- **Status pill:** icon + label + tint; never tint alone.
- **Review gate panel:** orthoview QC + key metrics + reviewer note + approve /
  redo / skip. Approve is the single accent button; redo amber-outline; reject
  danger-outline.
- **Log stream:** IBM Plex Mono, level-tinted left rule, calm contrast.
- **Buttons:** one accent (primary action) per view; everything else is
  outline/ghost. Active scale 0.98. No bounce easing.

## Motion

Subtle and functional only: 120–180ms ease-out for state changes, progress, and
panel entrance. The heartbeat pulse is the liveliest element. Honor
`prefers-reduced-motion: reduce` — drop to opacity/no transform.

## Accessibility

WCAG 2.1 AA contrast on all text and controls. Color never the sole signal.
Full keyboard path through run → review → approve. Visible focus rings
(functional box-shadow). Respect reduced-motion.
