# S1 — live repair, independently verified in preview

Export of the second and final live pilot. Nothing here was re-run to produce
it: the files are the stored records of the run that happened on 2026-09-19.
No credentials, no raw database dumps, no merge, no deployment, and the
coordinator is stopped.

`scripts/export_verification.py` writes them, given the repair and verification
ids explicitly and refusing a verification that belongs to another repair. It
then reads the published file back and runs the real
`portal.verification.grade()` on it, which must return `passed` over the S1 and
N1 cases or the export fails; that is where the numbers below come from.

| | |
| --- | --- |
| Incident | 2 — `omitted_row_limit_is_reset`, fingerprint `4850806e18c18de5acf83d2588df81c0` |
| Issue | https://github.com/woohyeokk-choi/superset/issues/3 |
| Session | https://app.devin.ai/sessions/18b04127f4a44af6a9c71f9eb3eaba9e |
| Pull request | https://github.com/woohyeokk-choi/superset/pull/4 |
| Tested head SHA | `fe266eac51a996760a75997ff94b3270c9ef73b1` |
| Baseline the PR branches from | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` (untouched) |
| Validator / automation ref | `86c03c333c57c42287db4c6efe268e7f140e4ebf` (pinned, agent cannot change it) |
| Fixture revision | `sha256:67ff039835f9890c`, content digest `47bea47eb499e2b929604bd1a5863a58`, 60 aggregate rows |
| Verdict | `passed` → repair state `verified_in_preview` (attempt 1, verification 9, 21:09:45.113–21:14:37.833 UTC) |
| Product follow-ups sent | 0 |
| Simulated | no (`simulated=0`) |

## How the incident was produced

Through the portal's own HTTP routes against the live baseline stack, not by
calling automation internals:

1. `POST /ops/fixtures/reset` (operator) restored the documented synthetic
   table chart. Read-back: chart 6, row limit **137**, palette
   `googleCategory10c`, sorted by `SUM(revenue)` descending.
2. `POST /settings/sort` (analyst) changed only the sort and sent **no**
   `row_limit` in the MCP `update_chart` config. Read-back: row limit
   **1000**, palette still `googleCategory10c`, sort ascending.

So the observed defect is the omitted field being reset to the schema default.
Palette preservation is a control that held throughout — there is no
colour-reset defect. A restricted-viewer action run through the portal in the
same window returned its expected authenticated denial and created **no**
incident; only the real failure did.

## What was actually checked

Both cases ran against the candidate stack at the PR head, not the baseline:
13 checks, all holding.

S1 — the target contracts, with setup that has to succeed first:

- `chart_is_created`, `chart_starts_at_the_saved_row_limit` (137),
  `chart_starts_with_the_saved_palette` (`googleCategory10c`)
- `the_requested_sort_change_persists` — the requested change still takes
  effect; preserving the row limit must not come from ignoring the update
- `an_omitted_row_limit_keeps_the_saved_value` — expected 137, observed 137

S1 controls, so the fix preserves normal behaviour rather than freezing the
field:

- `an_omitted_palette_keeps_the_saved_value` — `googleCategory10c`
- `control_an_explicit_row_limit_is_applied` — 275 asked for, 275 saved
- `control_an_explicit_schema_default_row_limit_is_applied` — an explicit
  1000 is still honoured, which a "never write 1000" fix would break
- `control_plain_chart_is_created` and
  `control_a_new_chart_keeps_the_schema_default_row_limit` — creation
  defaults unchanged

N1 — the authenticated permission control, denial only:

- `control_the_restricted_role_can_list_charts` (the login is real, HTTP 200)
- `the_restricted_role_is_denied_a_write`, `..._denied_chart_data` — HTTP 403.
  A 401 would be an authentication failure, not a passing control.

S2's defect is **not** required to pass here: that repair's PR is open and
unmerged, so its bug is still present in the baseline this candidate branches
from. Each repair is judged on its own registered cases.

## Provenance

Measured at verification time, not asserted by the agent:

- checkout `candidatefe266eac51a9` at the PR head, `clean: true`,
  code hash `df0861b8afe7263353effbf2b6ca0623`
- the same hash read from *inside* both the web and MCP containers, with their
  container ids, image id and the config path each one loaded
- source mounts point at the candidate checkout, never the baseline tree
- MCP readiness is a protocol `initialize` answering with its tool list
- candidate containers received no GitHub, Devin or controller credentials, and
  no host Docker socket

## Attempts

One attempt, one pass (`attempts.json`, scoped to repair 2): the candidate was
verified on the first try, and no follow-up was sent to the session because no
product check failed. Blocked attempts would appear here distinctly; there are
none for this repair.

## What the candidate changed

Three files, `superset/mcp_service/chart/chart_utils.py`,
`superset/mcp_service/chart/plugins/table.py` and
`tests/unit_tests/mcp_service/chart/tool/test_update_chart.py` (+107 lines),
all within the registered S1 change scope.

## What the repair agent reports running (self-reported, not independent)

From the pull request body, so it is the agent's account of its own machine — a
different environment from the verification stack, and no part of the verdict
above:

- `tests/unit_tests/mcp_service/chart`: **1862 passed, 1 skipped**
- its two new tests (`test_sort_only_update_preserves_omitted_row_limit`,
  `test_normalized_sort_only_update_preserves_omitted_row_limit`) reported as
  failing on the base branch with `assert 1000 == 137` and passing with the fix

## What was NOT run

- **No CI on the candidate.** The head SHA has **0 check-runs and 0 commit
  statuses** (read from GitHub at export time); the combined state reads
  `pending` only because nothing ever reported. That is the absence of CI, not
  a passing CI result.
- Nothing in this lifecycle ran Apache Superset's upstream workflows, `tox`,
  the full `pytest` suite or any frontend job.
- No merge, no deployment. `verified_in_preview` is the end state.
- No second session, no cap increase: `max_acu_limit=20` was sent on create,
  the API reported `acus_consumed: 0.0` at the final poll, and session reads
  carry no cap field to confirm server-side enforcement. ACUs are not dollars.
