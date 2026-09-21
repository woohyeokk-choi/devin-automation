# Exact-SHA local replay — S2 and S1

A **later replay**, recorded 2026-09-19 22:14–22:17 UTC, not the original
verification. The original independent verifications remain in
[`artifacts/phase6`](../../phase6) and are untouched. Nothing here started a
repair session, changed a repair state, or sent a notification.

Each case ran the **unchanged** authoritative scenario twice: once against a
fresh isolated stack at the Superset baseline, once against a fresh isolated
stack at the already accepted pull request head.

| | Baseline `394bca55c792b7b3547e23f6e175a7cb0f0757e8` | Candidate |
| --- | --- | --- |
| **S2** discarded `form_data` key reuse | key `K1` deleted (404), then exploration B reuses `K1` and resurrects it (200) — `reproduced: true` | `d234055eaf85a70d5e5ec5a7a7256ee43d02dde6` — `K2 != K1`, discarded `K1` stays 404, `reproduced: false` |
| **S1** sort-only MCP update | persisted `row_limit` 137 → 1000 — `reproduced: true` | `fe266eac51a996760a75997ff94b3270c9ef73b1` — `row_limit` 137 → 137, `reproduced: false` |

`color_scheme` stayed `googleCategory10c` in both S1 runs; its preservation is
a control, not part of the defect.

## Files

- `S2-exact-sha-replay.mp4`, `S1-exact-sha-replay.mp4` — ~13 s each, annotated,
  labelled on screen as a later replay.
- `screenshots/` — the failing and passing moment of each case, plus the
  light build's missing chart editor.
- `baseline/`, `s2/`, `s1/` — each run's `result.json` and sanitized
  `transcript.json` as written by `scenarios/_harness.py`.
- `*-environment.json` — the stack each run used: Compose project, loopback
  ports, checkout path, measured head SHA, clean-tree flag, per-service code
  hash, config revision and synthetic fixture digest.

The scenarios' own `reproduction.md` is written from the baseline's point of
view and is not copied here; `result.json`, `transcript.json` and the
environment manifests are the authoritative record.

## Limitations

- **No chart-editor UI.** The pinned light image carries no compiled frontend
  bundle, so `/login/` renders a spinner and the chart editor cannot be shown.
  The evidence is therefore the scenario output plus the persisted REST
  read-back of the chart, not a screen of the product's own controls. No
  frontend was built and no UI was simulated.
- The N1 permission control was not re-run in this replay; it is part of the
  original phase 6 verifications.
- These stacks were scratch namespaces on loopback ports and were torn down
  afterwards: no container, volume or checkout from them survives.
- Neither product pull request is merged, and nothing was deployed.
