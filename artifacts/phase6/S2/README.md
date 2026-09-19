# S2 — live repair, independently verified in preview

Export of the one real S2 run for independent review. Nothing here was re-run to
produce it: the files are the stored records of the run that happened on
2026-09-19, redacted through `portal.redaction.scrub` before commit. No
credentials, no raw database dumps, no new live run, and the coordinator is
stopped.

| | |
| --- | --- |
| Incident | 1 — `discarded_form_data_key_is_reused`, fingerprint `f7ff733a667fe52ceddc3eb0cff204c1` |
| Issue | https://github.com/woohyeokk-choi/superset/issues/1 |
| Session | https://app.devin.ai/sessions/4b7011c76c1e4ec8bf28cb373b614577 |
| Pull request | https://github.com/woohyeokk-choi/superset/pull/2 |
| Tested head SHA | `d234055eaf85a70d5e5ec5a7a7256ee43d02dde6` |
| Baseline the PR branches from | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` (untouched) |
| Validator / automation ref | `0ddca84740d985bb68d90697d1fdcade1295f4ed` (pinned, agent cannot change it) |
| Fixture revision | `sha256:67ff039835f9890c`, content digest `47bea47eb499e2b929604bd1a5863a58`, 60 aggregate rows |
| Verdict | `passed` → repair state `verified_in_preview` (attempt 1, verification 8) |
| Product follow-ups sent | 0 |
| Simulated | no (`simulated=0`) |

`verification-passed.json` is the whole record: every check with its expected and
observed value, the commands that built the candidate, the provenance measured
inside each container, and the usage the API actually reported.
`attempts.json` is all eight attempts in order.

## What was actually checked

Both cases ran against the candidate stack, not the baseline: 19 checks, all
holding.

S2 — the target contracts, with setup that has to succeed first (create/read A,
discard, 404, create B in one authenticated context):

- `new_exploration_does_not_reuse_a_discarded_key`
- `discarded_link_stays_dead_after_a_new_exploration`
- `the_new_exploration_reads_back_exactly_what_was_saved`

S2 controls, so the fix preserves normal behaviour rather than eliminating key
reuse altogether:

- `control_a_second_workspace_gets_its_own_key` / `..._reads_its_own_state`
- `control_same_context_save_without_discarding_updates_in_place`
- `control_same_context_reuse_serves_the_latest_state`

N1 — the authenticated permission control, denial only:

- `control_the_restricted_role_can_list_charts` (the login is real)
- `the_restricted_role_is_denied_a_write`, `..._denied_chart_data` — HTTP 403.
  A 401 would be an authentication failure, not a passing control.

S1 is not in this run. Its defect is still present in the baseline the candidate
branches from, and this repair targets one bug.

## Provenance

Measured at verification time, not asserted by the agent:

- checkout `candidated234055eaf85` at the PR head, `clean: true`,
  code hash `f5e7c645aa2f0284eb6fce4130bfe48f`
- the same hash read from *inside* both the web and MCP containers, with their
  container ids, image id and the config path each one loaded
- source mounts point at the candidate checkout, never the baseline tree
- MCP readiness is a protocol `initialize` answering with its tool list, not an
  inherited HTTP probe
- candidate containers received no GitHub, Devin or controller credentials, and
  no host Docker socket

## The eight attempts

Attempt 8 is the only pass. The seven before it are `blocked` — never
`passed`, never silently retried into a pass. Two kinds, kept distinct:

| # | Kind | What happened |
| --- | --- | --- |
| 1, 2 | Policy rejection | The candidate touched `tests/unit_tests/commands/explore/form_data/test_create.py`, outside the registered S2 scope. Fixed in the automation by widening the S2 family scope to the explore/form-data test paths — a scope registration gap, not a candidate fault. |
| 3 | Environment | Port bind conflict on the candidate web container. Fixed with per-candidate free-port allocation. |
| 4, 6 | Environment | `superset-init-light` exited 1 during candidate preparation. |
| 5, 7 | Environment | `git fetch` failed with `getcwd() failed` — the checkout was deleted underneath a running process. |

Attempts 3–7 all have the same root cause: four of my earlier
`portal.coordinator run` loops were alive over one live-state directory, each
calling `verify()` on the same repair and each deleting and recreating the same
candidate checkout and Compose project. Fixed by a single-instance advisory lock
per state directory (`coordinator.lock`). With one owner, attempt 8 passed.

Attempts 3–7 have no artifact file because they failed before the validator
produced a report; their stored `reason` is the runner error.

## Regression tests

Run on the automation repository at the current PR #1 head, on this machine:

- `python3 -m pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning`
  — **247 passed**, 1 warning (an unrelated Starlette deprecation in
  `tests/test_demo_gate.py::test_forged_profile_cookie_is_refused`)
- `python3 -m pytest tests/test_controller.py -k concurrent` repeated 120× with
  unhandled thread exceptions promoted to errors — **0 failures**. The same
  loop on the pre-fix code dropped a delivery in **1 of 60** runs.
- `python3 -m flake8 portal tests` — clean
- `python3 -m compileall -q portal tests` — clean

## What was NOT run

- **No CI on the candidate.** The head SHA has **0 check-runs and 0 commit
  statuses**; the combined state reads `pending` only because nothing ever
  reported. That is the absence of CI, not a passing CI result.
- Apache Superset's upstream workflows, `pre-commit` hooks, `tox`, the full
  `pytest` suite and any frontend job were not executed against the candidate.
  The only tests that ran on it are this validator's behavioural replay.
- No merge, no deployment. `verified_in_preview` is the end state.
- S1 was not triggered.
