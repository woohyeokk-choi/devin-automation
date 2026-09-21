# Submission

A runtime-repair loop around a fork of Apache Superset: a real user action
fails, structured server-side logs become a deduplicated incident, a
controller opens a fork issue and starts one Devin API session, that session
reproduces and fixes the product and opens a pull request, and an independent
host validator replays the same scenario against the exact PR head. Passing
means `verified_in_preview` — never merged, never deployed.

The **primary story is S1** — the omitted `row_limit` reset, run end to end in
namespace `fresh-demo-20260919-2310`. B1 (browser telemetry) is **optional and
disabled**: it is implemented and locally exercised, never dispatched, and
nothing in the main story depends on it.

## What answers which part

| Part | Where it is answered |
| --- | --- |
| 1 — detect a real runtime failure | The portal's registered behaviour check on a real MCP sort-only `update_chart` that omits `row_limit`: the saved limit is reset (137 → 1000 in the original incident). Sanitized events → one deduplicated incident (`omitted_row_limit_is_reset`). `portal/incidents.py`, console at `/ops/incidents`. |
| 2 — repair it automatically | The coordinator turns that incident into one fork issue and exactly one Devin API session (durable intent, single-flight claim, budget/deadline), the session reproduces and fixes Superset and opens a PR, and an independent host validator replays the scenario against the exact PR head. `portal/controller.py`, `portal/verification.py`. |
| 3 — observability | Operator console: per-run summary card (distinct incidents, repairs, linked PRs, independent preview passes, merge-verified, attention), per-incident lifecycle state, attempts, requested ACU vs reported consumption (unknown stays unknown), last recorded update with a no-fresh-poll warning, full verification reports, and a Slack thread with a durable delivery ledger. `portal/summary.py`, `/ops/incidents`, `/ops/notifications`. |

## Links

| | |
| --- | --- |
| Automation code | <https://github.com/woohyeokk-choi/devin-automation> — `main`, merged from [PR #1](https://github.com/woohyeokk-choi/devin-automation/pull/1), which keeps the full development history |
| Superset fork | <https://github.com/woohyeokk-choi/superset>, baseline `394bca55c792b7b3547e23f6e175a7cb0f0757e8` |
| Repair 1 (S2) | issue [#1](https://github.com/woohyeokk-choi/superset/issues/1) → session [`4b7011c7…`](https://app.devin.ai/sessions/4b7011c76c1e4ec8bf28cb373b614577) → PR [#2](https://github.com/woohyeokk-choi/superset/pull/2) @ `d234055eaf85a70d5e5ec5a7a7256ee43d02dde6` |
| Repair 2 (S1) | issue [#3](https://github.com/woohyeokk-choi/superset/issues/3) → session [`18b04127…`](https://app.devin.ai/sessions/18b04127f4a44af6a9c71f9eb3eaba9e) → PR [#4](https://github.com/woohyeokk-choi/superset/pull/4) @ `fe266eac51a996760a75997ff94b3270c9ef73b1` |
| Fresh run (S1, `fresh-demo-20260919-2310`) | issue [#5](https://github.com/woohyeokk-choi/superset/issues/5) → session [`e8b63e60…`](https://app.devin.ai/sessions/e8b63e608d5a4397b114a296420695c7) → PR [#6](https://github.com/woohyeokk-choi/superset/pull/6) @ `7bb8de7b136f9afdc39d31b4c6809b3de467c461`, **open, unmerged, awaiting a human** |
| Fresh-run Slack | three status posts written by the coordinator as the run happened (investigating 23:17:41Z, pull request 23:32:35Z, verified-awaiting-merge 23:39:45Z; re-rendered as Block Kit cards at 23:58Z) — unlike the two older S2/S1 messages, which are backfills marked *Historical result*. Outbound status only, never paging |
| Fresh-run media | symptom clips `F0C324CM3KQ` (portal action) and `F0C2XU6V475` (Superset's own Explore, baseline), plus one candidate **preview** clip `F0C36VA3HQA` — Superset's own Explore at unmerged PR #6 head `7bb8de7b136f`, saved limit 10 kept and the requested descending sort applied, recorded by local replay on a loopback stack. The after-merge clip does not exist yet |
| Native chart preview | `artifacts/fresh-demo-20260919-2310/visual-preview/` — the same defect in Superset's Explore UI on the baseline, and held at candidate `7bb8de7b136f`; labelled PREVIEW |
| Code tour (two anchors) | [API create/manage boundary](https://github.com/woohyeokk-choi/devin-automation/blob/369c153fdc48c6289da772a55b6d52819a77c6ff/portal/controller.py#L762-L798) — one durable intent per attempt, reconcile-before-retry, one session; [exact-SHA boundary](https://github.com/woohyeokk-choi/devin-automation/blob/369c153fdc48c6289da772a55b6d52819a77c6ff/portal/verification.py#L404-L481) — a verdict is refused unless the containers, checkout, fixture and assets measurably belong to the PR head |
| Superset diff anchor | [`chart_utils.py` L896–903](https://github.com/woohyeokk-choi/superset/blob/7bb8de7b136f9afdc39d31b4c6809b3de467c461/superset/mcp_service/chart/chart_utils.py#L896-L903) + [`plugins/table.py` L131–133](https://github.com/woohyeokk-choi/superset/blob/7bb8de7b136f9afdc39d31b4c6809b3de467c461/superset/mcp_service/chart/plugins/table.py#L131-L133) and [three regression tests](https://github.com/woohyeokk-choi/superset/blob/7bb8de7b136f9afdc39d31b4c6809b3de467c461/tests/unit_tests/mcp_service/chart/tool/test_update_chart.py#L146-L240): 103 added lines, no deletions |
| Evidence | `artifacts/phase6/S2/`, `artifacts/phase6/S1/` |
| B1 (browser telemetry, no dispatch) | [apache/superset#44007](https://github.com/apache/superset/issues/44007) reproduced on the baseline; monitor scans, admission modes and the validator's baseline failure in `artifacts/b1-monitor/`. No issue, session, PR or Slack post exists for it. |
| Exact-SHA replay clips | `artifacts/phase8/replay/` — baseline vs accepted head per case, recorded later, not the original verification |
| Live demonstration chart | `Order revenue - live demonstration` (chart 10, row limit 10, raw records): a sort-only update takes it to limit 1000 and ~600 rendered rows. A **local demonstration replay with dispatch disabled** — a separate fixture from the 137 → 1000 incident, never counted with it |
| Numbers, with the caveats | [docs/results.md](results.md) |
| Design decisions and full history | [docs/EXECUTION_PLAN.md](EXECUTION_PLAN.md) |

The automation `main` branch carries the application; every Superset repair
pull request, including #6, stays **open and unmerged**, and nothing in this
repository implies otherwise.

## Try it in one command, no credentials

```bash
git clone https://github.com/woohyeokk-choi/devin-automation.git && cd devin-automation
docker build -t runtime-repair-portal .
PORTAL_DATA_DIR=$PWD/runtime/sim PORTAL_UID=$(id -u) PORTAL_GID=$(id -g) \
  python3 scripts/check_shared_state.py     # "shared state check: PASS"
```

The real portal image runs under `--network none`; the controller half runs
against `FakeGitHub`/`FakeDevin`. Everything it reports is **simulated** and
no request leaves the machine. A recorded run from a clean clone, with image
digest and ref, is in `artifacts/phase7/simulation/`. The full stack setup is
in the [README](../README.md).

## The claim, precisely

Verified: two genuine product defects, found from real failing user actions,
were fixed by API-created sessions and each passed an independent behavioural
replay at its exact PR head — 19 checks for S2, 13 for S1, all holding, zero
product follow-ups needed. Most of those checks are setup and controls, and
the two runs share three permission checks; they are not 32 unique tests.

Third defect, B1: the loop's entry point no longer has to be an assertion the
automation wrote. A scheduled monitor drives its own Chromium through the
saved chart read-only and admits the product's own `console.warn` — a handled
formatter failure with a visible fallback, not an exception or a crash. It is
implemented and exercised locally; it has not been dispatched, so there is no
B1 issue, session, PR or notification, and its verifier has run only against
the unfixed baseline, where it correctly fails.

Not verified: no upstream CI ran on either candidate (0 check-runs, 0
statuses), nothing is merged or deployed, human touch time and cost per repair
were not measured, the API's `acus_consumed: 0.0` is reported exactly as
returned rather than as a cost claim, and Slack is outbound status only — a
webhook notifier with a durable ledger, no Q&A, no exactly-once guarantee. The two
older S2/S1 results were announced after the fact as backfills; in the
recorded `fresh-demo-20260919-2310` run the coordinator posted each
transition while the run was in progress. Five messages reached the channel
rather than the three authorized: two were simulated lifecycle lines posted by
the test suite, which inherited the ambient webhook; simulated wiring now
refuses a real transport at runtime, proven by a canary-webhook regression
with sockets intercepted.

## Safety boundaries worth knowing

- The portal is pinned to `AUTO_REPAIR_ENABLED=false`; only the trusted host
  coordinator holds credentials, git and Docker.
- Candidate stacks get no credentials and no Docker socket.
- One active repair at a time (a SQLite claim row), one coordinator per state
  directory (an advisory lock), at most two same-session feedbacks, a
  120-minute deadline.
- A verification is only usable if the PR head repository and full SHA match
  and the in-container code hash matches what was read from GitHub.
