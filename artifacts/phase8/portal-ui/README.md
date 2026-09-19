# S1 custom-portal UI replay (later local capture)

Two screen recordings of the **operator portal's own UI** — this project's
FastAPI/Jinja `/settings` page — driving real isolated Superset stacks over
REST/MCP. They are the UI counterpart to the CLI/API clips in
`../replay/`, captured later and locally; they are **not** the original
phase 6 independent verification, **not** Superset's own chart editor, and
**not** a deployment.

| Clip | Superset commit | Visible capture time (UTC) | Length |
| --- | --- | --- | --- |
| `S1-portal-ui-baseline.mp4` | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` | 2026-09-19T22:52:15Z | 17.75 s |
| `S1-portal-ui-candidate.mp4` | `fe266eac51a996760a75997ff94b3270c9ef73b1` | 2026-09-19T22:53:07Z | 16.25 s |

Each clip shows its own `git rev-parse HEAD` and the capture time in a
terminal before the action, so the commit a clip ran against is a property
of the footage rather than of the repair it is attached to.

## The action, and what each side does with it

Starting state on both sides: saved row limit **137**, highest revenue
first, `googleCategory10c`, and the optional row-limit input left empty.
The action is the same on both: change only the sort to lowest revenue
first, submit.

* Baseline `394bca55`: the saved row limit is silently reset **137 → 1000**
  — the defect S1 reported.
* Accepted head `fe266eac`: the identical action preserves **137 → 137**.
* Both sides save ascending `SUM(revenue)` and leave `googleCategory10c`
  untouched, so the change is scoped to the omitted field.

Stored traces (`trace_30e9d378296d` baseline, `trace_92c41f8fd98e`
candidate) record `explicit_row_limit=""` on the request and no `row_limit`
in the MCP update, which is the condition the fix is about.

## Scope and limits

* S1 only. S2 and N1 were not re-run here, and neither was the
  explicit-row-limit regression.
* The UI is this project's portal, not Superset's frontend, which the
  pinned light image does not build.
* A later replay, not the verification run the accepted heads were judged
  by; that evidence is in `../../phase6/`.
* No merge, no deployment, no public exposure.

## Conditions of the capture

Dispatch was off, neither Slack variable was present in the portal or stack
processes, and the portal's network guard recorded no blocked outbound
attempt: no coordinator, Devin session, Slack call or live-state write took
place. Both isolated stacks and both scratch checkouts were torn down
afterwards. Portal code was at automation `8759e7a` when the stacks were
prepared and advanced to `32e64cb` during setup, with no change to the
settings page, app, domain, config or templates involved.

`baseline-environment.json` and `candidate-environment.json` are the
measured per-service identities of the two stacks; `SHA256SUMS` covers every
file here; the `*-annotations.json` files are the on-screen annotations.

## Screenshots

| | Baseline | Candidate |
| --- | --- | --- |
| before | `screenshots/ss_77d963e6.png` | `screenshots/ss_a182f205.png` |
| after | `screenshots/ss_2a7e598e.png` (1000) | `screenshots/ss_f020efed.png` (137) |
| commit and time | `screenshots/ss_8e7ed492.png` | `screenshots/ss_6b123316.png` |
