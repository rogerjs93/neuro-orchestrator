# UI/UX Parity Checklist

Goal: improve visual design without changing behavior semantics.

## 1. Global constraints
- No API path changes.
- No request/response schema changes.
- No JS event hook ID changes for existing controls.
- No removal of existing user actions.
- No keyboard shortcut behavior changes.

## 2. Dashboard route `/`

### 2.1 Data + run controls
- Upload BIDS zip still works.
- Run selected subject still works.
- Run all still works.
- Run single stage still works.
- Reset pipeline still works.

### 2.2 Status + logs
- Connection indicator updates correctly.
- Subject status rows update correctly.
- Stage status chips update correctly.
- Log stream remains readable and live.

### 2.3 STL workflow
- STL preset selection still works.
- STL queue/launch actions still work.
- Deferred STL behavior still works.
- Manual editor navigation links still work.

## 3. Manual mask route `/manual-mask`

### 3.1 Initialization + navigation
- Back to dashboard works.
- Init from auto works.
- Init empty mask works.
- Refresh catalog/version state works.

### 3.2 Editing + history
- Paint operation works.
- Erase operation works.
- Stroke paint works.
- Stroke erase works.
- Morphology actions work.
- Undo works.
- Redo works.

### 3.3 Export + safety
- Export STL from active version works.
- Delete current version keeps server safety checks.
- Conflict resolution on stale parent still works.

## 4. Non-functional checks
- Focus visibility is clear on all interactive controls.
- Keyboard traversal remains usable.
- Disabled/loading/error states are visible.
- Mobile layout remains functional.
- Reduced-motion users are respected.

## 5. Validation commands
- `npm run ui:skill:install`
- `python scripts/manual_mask_smoke.py --base-url http://localhost:8080 --subject sub-001`
