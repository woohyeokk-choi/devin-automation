# Submission

A runtime-repair loop around a fork of Apache Superset: a real user action
fails, structured server-side logs become a deduplicated incident, a
controller opens a fork issue and starts one Devin API session, that session
reproduces and fixes the product and opens a pull request, and an independent
host validator replays the same scenario against the exact PR head. Passing
means `verified_in_preview` — never merged, never deployed.

## Links

| | |
| --- | --- |
| Automation code | <https://github.com/woohyeokk-choi/devin-automation/pull/1> (branch `devin/1789830208-phase1-baseline-reproductions`; `main` does not contain the app) |
| Superset fork | <https://github.com/woohyeokk-choi/superset>, baseline `394bca55c792b7b3547e23f6e175a7cb0f0757e8` |
| Repair 1 (S2) | issue [#1](https://github.com/woohyeokk-choi/superset/issues/1) → session [`4b7011c7…`](https://app.devin.ai/sessions/4b7011c76c1e4ec8bf28cb373b614577) → PR [#2](https://github.com/woohyeokk-choi/superset/pull/2) @ `d234055eaf85a70d5e5ec5a7a7256ee43d02dde6` |
| Repair 2 (S1) | issue [#3](https://github.com/woohyeokk-choi/superset/issues/3) → session [`18b04127…`](https://app.devin.ai/sessions/18b04127f4a44af6a9c71f9eb3eaba9e) → PR [#4](https://github.com/woohyeokk-choi/superset/pull/4) @ `fe266eac51a996760a75997ff94b3270c9ef73b1` |
| Fresh run (S1, `fresh-demo-20260919-2310`) | issue [#5](https://github.com/woohyeokk-choi/superset/issues/5) → session [`e8b63e60…`](https://app.devin.ai/sessions/e8b63e608d5a4397b114a296420695c7) → PR [#6](https://github.com/woohyeokk-choi/superset/pull/6) @ `7bb8de7b136f9afdc39d31b4c6809b3de467c461`, **open, unmerged, awaiting a human** |
| Fresh-run media | symptom clips delivered (Slack `F0C324CM3KQ` portal action, `F0C2XU6V475` native Explore replay); the after-merge clip does not exist yet |
| Native chart preview | `artifacts/fresh-demo-20260919-2310/visual-preview/` — the same defect in Superset's Explore UI on the baseline, and held at candidate `7bb8de7b136f`; labelled PREVIEW |
| Evidence | `artifacts/phase6/S2/`, `artifacts/phase6/S1/` |
| Exact-SHA replay clips | `artifacts/phase8/replay/` — baseline vs accepted head per case, recorded later, not the original verification |
| Numbers, with the caveats | [docs/results.md](results.md) |
| Design decisions and full history | [docs/EXECUTION_PLAN.md](EXECUTION_PLAN.md) |
| Loom walkthrough | _pending — to be recorded_ |

## Try it in one command, no credentials

```bash
git clone --branch devin/1789830208-phase1-baseline-reproductions \
  https://github.com/woohyeokk-choi/devin-automation.git && cd devin-automation
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

Not verified: no upstream CI ran on either candidate (0 check-runs, 0
statuses), nothing is merged or deployed, human touch time and cost per repair
were not measured, the API's `acus_consumed: 0.0` is reported exactly as
returned rather than as a cost claim, and Slack is outbound status only — a
webhook notifier with a durable ledger, no Q&A, no exactly-once guarantee, and
no message sent during either live run. Five messages reached the channel
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
