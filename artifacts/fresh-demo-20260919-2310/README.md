# Fresh run `fresh-demo-20260919-2310` — evidence

One incident, one issue, one repair session, one candidate pull request, one
independent preview verification, one delivered symptom clip, and a human
merge gate that has not been passed.

| | |
| --- | --- |
| Baseline | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` |
| Fingerprint | `4850806e18c18de5acf83d2588df81c0` (family `omitted_row_limit_is_reset`) |
| Issue | <https://github.com/woohyeokk-choi/superset/issues/5> |
| Session | <https://app.devin.ai/sessions/e8b63e608d5a4397b114a296420695c7> |
| Candidate | PR [#6](https://github.com/woohyeokk-choi/superset/pull/6) @ `7bb8de7b136f9afdc39d31b4c6809b3de467c461`, base `runtime-repair/fresh-demo-20260919-2310` |
| Budget | 20 ACUs requested, deadline `2026-09-20T01:17:41Z` |
| Lifecycle state | `awaiting_merge` — **not merged, not deployed** |

## Contents

- `lifecycle.json` — the stored repair row, the incident, the three Slack
  lifecycle messages with their timestamps, and the upload ledger row for the
  symptom file. Copied from the live state, not retyped.
- `preview-verification/` — the independent host validator's record for the
  candidate head: `passed` over 13 registered checks, exported from the stored
  report with the same redaction the console uses. Preview only; it says
  nothing about merged code.
- `visual-preview/` — the same defect in Superset's own Explore UI on the
  baseline, with measured row counts, plus the same scenario replayed against
  candidate head `7bb8de7b136f` (limit 10 held, 10 rows, requested order
  applied). Labelled PREVIEW; see its own README.

## The two media stages

`symptom` is delivered: Slack file `F0C324CM3KQ`, message
`1789863673.363649` in the incident thread `1789859861.249849`, captured by
the repair session at `20260920T001427Z` against the baseline, one upload row,
never retried. A supplemental later replay of the native Explore presentation
chart, recorded on the builder VM, is Slack file `F0C2XU6V475` in the same
thread — still symptom footage, still not post-merge evidence.

`post-merge` does not exist. It requires a human merge, a retained loopback
deployment rebuilt from GitHub's merge commit, the running source measured to
equal that commit, and the registered checks passing there. Nothing in this
directory may be presented as that evidence.
