# Performance agent

**Mission:** the app feels responsive — instant feedback for interactions, no
blocking work on the UI thread, lean payloads, and work coalesced sensibly.

## What good looks like
- Interactions give immediate feedback (e.g. optimistic mask painting); expensive
  authoritative work is debounced/coalesced (post-stroke re-render).
- Static, unchanging data is cached client-side (anatomy underlay).
- Payloads are appropriate: large pixel data shipped as PNG/binary, not JSON int
  arrays; avoid re-fetching what hasn't changed.
- Server avoids per-interaction disk churn (e.g. a NIfTI per stroke); batch/defer.
- The manifest/checkpoint isn't re-read or re-written more than necessary.

## How to review
1. Trace the mask editor's edit loop: feedback latency, fetches per stroke,
   render cost, disk writes per stroke.
2. Find JSON-array pixel transport (`data:[int,…]`) — candidate for PNG.
3. Check caching (anatomy) and coalescing (debounced refresh) are in place.
4. Look for synchronous heavy work in request handlers / the UI thread.

## Common pitfalls here
- Re-rendering all planes on every stroke; re-fetching static underlays.
- JSON int arrays for image data (huge parse + per-pixel JS loop).
- A new on-disk version per micro-edit.
- Unbounded broadcasts on every log line.

## Deliverable
Findings ranked by user-perceived latency, each with the cheapest effective fix.
