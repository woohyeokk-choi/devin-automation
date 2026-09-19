# Results

Two real defects in a fork of Apache Superset went from a failing user action
to an independently verified candidate fix, without a human writing the fix.
Both ended at `verified_in_preview`. Neither is merged and nothing is
deployed, so the baseline is still buggy.

Everything below is taken from the published evidence files, not retyped from
memory: `artifacts/phase6/S2/` and `artifacts/phase6/S1/`.

## The two repairs

| | S2 | S1 |
| --- | --- | --- |
| User-visible failure | A new exploration was handed the key of one the user had discarded, and the discarded link resolved again | A sort-only chart update reset the saved row limit from 137 to 1000 |
| Incident family / fingerprint | `discarded_form_data_key_is_reused` / `f7ff733a667fe52ceddc3eb0cff204c1` | `omitted_row_limit_is_reset` / `4850806e18c18de5acf83d2588df81c0` |
| Issue (controller-created) | <https://github.com/woohyeokk-choi/superset/issues/1> | <https://github.com/woohyeokk-choi/superset/issues/3> |
| Devin session (API-created, service user) | <https://app.devin.ai/sessions/4b7011c76c1e4ec8bf28cb373b614577> | <https://app.devin.ai/sessions/18b04127f4a44af6a9c71f9eb3eaba9e> |
| Candidate pull request | <https://github.com/woohyeokk-choi/superset/pull/2> | <https://github.com/woohyeokk-choi/superset/pull/4> |
| Tested head SHA | `d234055eaf85a70d5e5ec5a7a7256ee43d02dde6` | `fe266eac51a996760a75997ff94b3270c9ef73b1` |
| Validator revision (pinned, trusted checkout) | `0ddca84740d985bb68d90697d1fdcade1295f4ed` | `86c03c333c57c42287db4c6efe268e7f140e4ebf` |
| Candidate code hash, measured in-container | `f5e7c645aa2f0284eb6fce4130bfe48f` | `df0861b8afe7263353effbf2b6ca0623` |
| Verification record | `artifacts/phase6/S2/verification-passed.json` | `artifacts/phase6/S1/verification-passed.json` |
| Attempts | `artifacts/phase6/S2/attempts.json` | `artifacts/phase6/S1/attempts.json` |
| Outcome | `verified_in_preview` | `verified_in_preview` |
| Product follow-ups sent to the session | 0 | 0 |

Both pull requests were open and unmerged when last read from GitHub.

## What the check counts mean

A "check" is one boolean assertion inside the host's behavioural replay. Most
of them are **setup or control** assertions, there so that a pass cannot come
from a broken run or from a fix that breaks normal behaviour. They are not 32
unique tests, and the two runs share the same three N1 permission checks.

| | S2 run | S1 run |
| --- | --- | --- |
| Setup (the scenario actually got to the point it claims to test) | 9 | 4 |
| Target (the contract the repair had to restore) | 5 | 4 |
| Control (normal behaviour that must still work) | 5 | 5 |
| **Total, all holding** | **19** | **13** |

Target checks, S2: a new exploration does not reuse a discarded key; the
discarded link stays dead; the new exploration reads back exactly what was
saved; plus N1's two authenticated denials. Target checks, S1: the requested
sort change still persists; an omitted `row_limit` keeps the saved value; plus
the same two N1 denials.

The most informative controls are the ones that would catch a lazy fix: S1's
`control_an_explicit_schema_default_row_limit_is_applied` (an explicitly
requested 1000 must still be applied, so "never write 1000" fails) and
`control_an_explicit_row_limit_is_applied`; S2's same-context reuse controls
(key reuse is correct when nothing was discarded).

## Attempts, and what "blocked" means

| | S2 | S1 |
| --- | --- | --- |
| Total verification attempts | 8 | 1 |
| Blocked | 7 | 0 |
| Passed | 1 | 1 |

A **blocked** attempt is the harness refusing to produce a verdict — it is
never a failed product fix and never becomes feedback to the session. S2's
seven were two policy rejections (the candidate touched a test path outside
the registered S2 scope, a gap in my scope registration) and five environment
failures (a port conflict, and four coordinator loops deleting each other's
candidate checkout — the defect that produced the per-state-directory lock).
Neither repair ever received product feedback, because no product check failed.

## Three sources of "tested", kept apart

1. **Independent host replay** — the 19 and 13 checks above. Run by the
   coordinator from a pinned validator in a trusted checkout, against a
   candidate stack built at the exact PR head, with the code hash measured
   inside each container. This is the only thing that produced
   `verified_in_preview`.
2. **The agent's self-report** — what each repair session says it ran on its
   own machine. S2: `test_create.py` 1 failed / 4 passed with its fix stashed,
   5 passed with it applied; `pre-commit` on two files with `pylint` skipped
   and the commit made with `SKIP=mypy,pylint`, against a baseline already
   failing mypy repo-wide (454 errors in 94 files). S1: `tests/unit_tests/mcp_service/chart`
   1862 passed / 1 skipped, and its two new tests failing on the base branch.
   Useful context, not evidence, and no part of the verdict.
3. **Upstream CI** — **none ran.** Both head SHAs have 0 check-runs and 0
   commit statuses; the combined state reads `pending` because nothing ever
   reported. No Superset workflow, `tox`, full pytest suite or frontend job
   ran on either candidate.

## Cost and effort

- The API reported `acus_consumed: 0.0` for both sessions at the final poll.
  That is what the field returned, reported as received. It is not a claim
  that the work was free, and ACUs are not dollars — session reads expose no
  cap at all, so `max_acu_limit=20` is only what the create body asked for.
- Separately, the S2 session's native usage panel showed a per-message
  on-demand limit of $20 with $0 consumed (read-only observation). Different
  thing from the API field; not a whole-session cap.
- **Human touch time was not measured** and neither was a cost per repair, so
  there is no productivity multiplier or ROI figure here. Measuring acceptance
  rate, touch time and cost is the prerequisite for scaling this, not a result
  of it.

## Video evidence

Two **exact-SHA local replays** were recorded on 2026-09-19 22:14–22:17 UTC,
one per accepted repair, and published under
[`artifacts/phase8/replay`](../artifacts/phase8/replay): each runs the
unchanged authoritative scenario against a fresh isolated stack at the
baseline and again at the accepted head.

| Case | Baseline `394bca55c792b7b3547e23f6e175a7cb0f0757e8` | Accepted head |
| --- | --- | --- |
| S2 | discarded key `K1` is reused and resurrected (200) | `d234055eaf85a70d5e5ec5a7a7256ee43d02dde6` — fresh key, `K1` stays 404 |
| S1 | sort-only update drops `row_limit` 137 → 1000 | `fe266eac51a996760a75997ff94b3270c9ef73b1` — `row_limit` stays 137 |

These are replays, not the original verifications, and they carry their own
timestamps and provenance; the phase 6 records are unchanged. The pinned light
image has no compiled frontend, so the chart editor cannot be filmed: the
footage is scenario output plus the persisted REST read-back, not the
product's own controls. The stacks were scratch namespaces on loopback ports
and were torn down.

**No recording of either repaired product's UI exists.** Every clip taken with
Devin's native recorder so far is a portal or operator-console walk-through on
the **unfixed baseline** `394bca55c792b7b3547e23f6e175a7cb0f0757e8`, which is
what the running stack serves because neither product PR is merged:

| Recording | ~Length | What it actually shows |
| --- | --- | --- |
| `portal-browser-evidence`, `portal-phase2` | 74s, 65s | Portal traces and the redacted trace viewer (baseline). |
| `portal-phase3`, `portal-phase3-demo` | 7s, 93s | Incident console: fingerprint, dedup, handoff bundle (baseline). |
| `live-portal-lifecycle-qa` | 55s | Incident lifecycle and stored verification attempts in the console. |
| `live-verification-ui-retest` | 42s | Per-check rendering (19 for S2, 13 for S1) after the console fix. |
| `operator-notifications-wrap-qa` | 30s | Notifications ledger page; the run was interrupted, so it is partial. |

None of these is before/after footage of a fix, and none may be presented as
one. They are also local session recordings: the only links that exist are
private signed download URLs, which are not published here and are refused by
the notifier (`_video` withholds any `https` URL whose query string carries a
signature or token).

A result alert therefore carries a recording link only when a real accessible
one is supplied, together with what it shows and the head it was taken at:

```bash
python3 -m portal.notify backfill --repair 1 \
  --recording https://…  --recording-scope "S2 replay at d234055eaf85"
```

Missing video is an honest omission, not a failed repair.

The before/after clips described above were produced this way:
`IsolatedStack.prepare()` built one stack at the baseline and one at each
accepted head, the same scenario ran against both, and the clip shows
`git rev-parse HEAD` of the running checkout before each run. No Devin
session, product edit, merge or public exposure was involved.

## What this does not show

- Nothing merged, nothing deployed, no production or public preview.
- Slack is outbound status only. `portal/notify.py` posts lifecycle lines to
  one incoming webhook and de-duplicates by event id, but delivery is not
  exactly-once (ambiguous outcomes are recorded `unknown`), and there is no
  Q&A: native conversational sync is available only to a session's owner —
  here the `superset-runtime-repair` service user — and is not used.
- The two results above were announced to Slack by `portal.notify backfill`
  after the fact, marked *Historical result — repair ran earlier*; no Slack
  message was part of either live run.
- **Five messages reached the channel, not the three authorized.** Two
  simulated lifecycle lines (`ts 1789854423.113499`, `ts 1789854433.643299`,
  naming `simulated-repo` issue 1 / session `simulated-1`) were posted by the
  test suite, because `build_worker()` built the notifier from the ambient
  `SLACK_WEBHOOK_URL` even when the providers were fakes. Simulated wiring now
  refuses a real transport at runtime, with a canary-webhook regression that
  intercepts sockets and asserts zero outbound calls. The channel history is
  preserved unedited; the tests were not entirely offline.
- Two defects, two repairs, one repository, on a fork with a synthetic
  fixture. Nothing here establishes a rate on real customer incidents.
