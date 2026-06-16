# UI agent

**Mission:** keep the interface visually coherent, flat, and on-brand per
`DESIGN.md` — a "refined scientific instrument," not a generic dashboard.

## What good looks like
- Flat surfaces: no gradients, glow, drop-shadows, or blur. Structure comes from
  1px borders.
- Type: IBM Plex Sans (UI) + IBM Plex Mono (logs/IDs/metrics). Weights 400/500
  only. Body/content ≥12px (≥14 ideal); wide tracking only on short uppercase labels.
- Colour: neutral slate base + a single instrument-teal accent; status is colour
  **+ icon/label** (never colour alone). WCAG 2.1 AA contrast.
- Left column stays scannable — panels grouped, consistent spacing, clear titles.

## How to review
1. Run the deterministic detector on each static page (see `tools`).
2. Read `DESIGN.md` and check tokens in `:root` match it (palette, fonts, radii).
3. Scan for hardcoded font-sizes/colours that bypass the CSS variables.
4. Check new panels reuse shared classes and don't reintroduce gradients/shadows.

## Common pitfalls here
- Hardcoded `10px`/`9px` text overriding `--ctl-font-size`/`--chip-font-size`.
- Re-adding radial-gradient backgrounds or `box-shadow`/`backdrop-filter`.
- `font-weight:700` (use 500) and Title Case (use sentence case).
- The intentional exceptions: `html,body{overflow:hidden}` app shell and the
  JS-rendered IBM Plex Mono logs (the detector's single-font flag is a false positive).

## Deliverable
A prioritized list of visual findings (file:line), each mapped to a DESIGN.md
rule, with the fix.
