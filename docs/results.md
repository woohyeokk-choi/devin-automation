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

A result alert therefore carries a recording link only when the capture
itself states what it recorded: the case, the full revision it ran against,
when it was taken, and what it shows. That revision is compared to the
accepted head; it is never inferred from the repair the clip is attached to,
so a link recorded elsewhere is reported as `recording pending — the capture
ran against …, not the head …` rather than published as verified footage.
Incomplete metadata, an abbreviated sha and a signed download URL are pending
for the same reason.

```bash
python3 -m portal.notify result --repair 1 \
  --recording https://… \
  --recording-case S2 \
  --recording-sha d234055eaf85a70d5e5ec5a7a7256ee43d02dde6 \
  --recording-at 2026-09-19T22:15:30+00:00 \
  --recording-scope "baseline failure then the accepted head passing"
```

Missing video is an honest omission, not a failed repair.

An existing local clip can be attached to its repair's thread with
`portal.notify attach --repair <id> --clip <path> …`, which requires the same
capture metadata, refuses a clip whose revision is not the accepted head, and
reserves a durable upload row before the first request so a repeated run or a
restart cannot publish the same file twice. Matching the head only says what
was recorded, so the upload also has to clear the acceptance gate a result
message clears — a non-simulated repair in `verified_in_preview` with a
stored attempt that passed on exactly that head — enforced in
`Notifier.attach` rather than in the CLI, so a candidate, a blocked
verification or an attempt that measured another commit never becomes a video
presented as accepted proof. A signed download URL is a
credential and is never uploaded or written down; only a local path an
operator names is.

The before/after clips described above were produced this way:
`IsolatedStack.prepare()` built one stack at the baseline and one at each
accepted head, the same scenario ran against both, and the clip shows
`git rev-parse HEAD` of the running checkout before each run. No Devin
session, product edit, merge or public exposure was involved.

## The fresh run's two video stages

The fresh incident (`fresh-demo-20260919-2310`, issue #5, session
`e8b63e608d5a…`, candidate PR #6) carries two separate media stages. They are
separate because they answer different questions, and neither can stand in for
the other.

**Symptom — delivered.** The same repair session recorded its own baseline
reproduction before touching product code, and the file was fetched through the
official session-attachment route and posted automatically into the incident
thread: Slack file `F0C324CM3KQ`, message `1789863673.363649` under parent
`1789859861.249849`, 20 seconds, captioned *Symptom replay — recorded after
detection · S1 captured 20260920T001427Z against `394bca55c792`*. It shows the
saved row limit going 137 → 1000, on the pinned baseline. It is a reproduction
recorded after detection, not footage of the customer's original moment, and it
says nothing about the fix. One upload row exists for it; it is never retried.

A second symptom clip sits beside it: Slack file `F0C2XU6V475`, 23 seconds,
captioned *Superset UI symptom replay — recorded after detection*. It is a
later replay recorded on the builder VM, showing the raw-records presentation
chart in Superset's own Explore UI — row limit 10 and 10 rows becoming limit
1000 and 600 rows after a sort-only update. Builder VM, later replay,
supplemental: it is neither the original discovery nor post-merge evidence, and
it does not touch the canonical 137-row case.

**After merge — does not exist.** PR #6 is open and unmerged, so there is no
merged SHA to deploy, verify or film. The after-video is only produced once a
human merges, the retained loopback deployment is rebuilt from GitHub's merge
commit, the running source is measured to equal it, and the registered checks
pass there. Until all of that has happened the lifecycle shows
`awaiting_merge`, and no preview, historical or portal-settings clip may be
relabelled as post-merge evidence.

A capture that is requested is not captured, and a captured file is not
delivered: delivery is claimed only from an upload ledger row holding a Slack
file id. Where capture is unavailable the stage stays visibly `pending` or
`failed` rather than being omitted.

### Seeing the defect in Superset's own chart

The canonical S1 assertion is a settings read-back, and its chart groups by
`region` — four values — so the reset changes no rendered row. To show a
customer what the bug costs them,
`artifacts/fresh-demo-20260919-2310/visual-preview/` captures the native
Explore UI on a separate presentation chart (`Order revenue - 10-row view
(presentation)`, raw records over `synthetic_orders`, row limit 10, ordered by
`revenue`): a sort-only update with no row limit resets the limit to 1000 and
expands the 10-row table to the whole 600-row table, measured from Superset's
row badge, not assumed. The frontend was compiled from the pinned source with
the repository's own `superset-node` Docker target; no upstream file was
changed.

The same chart was replayed against candidate head `7bb8de7b136f` in an
isolated stack whose containers both measure to that exact commit: the row
limit stays 10, ten rows render, and the requested `revenue [desc]` order
applies — read back from the chart API and confirmed in Explore
(`candidate-7bb8de7b136f-raw-presentation.json`,
`candidate-7bb8de7b136f-05-after-sort-only.png`). That is a preview of an open
pull request and a supplemental check; the 13 registered checks and the 137-row
fixture are unchanged by it.

Raw records rather than an aggregate top-N deliberately. In aggregate mode the
table plugin rebuilds `orderby` from the metric, so a requested sort never
reaches the rendered chart — a separate behaviour PR #6 does not touch, and
one this demonstration must not appear to claim. In raw mode the requested
ordering does apply, leaving the lost row limit as the only thing on show.

Those frames are **PREVIEW, on the baseline, on the builder's isolated
loopback stack** — a different machine from the repair session's VM. The
verdict for this scenario lives in `portal/visual.py` and is labelled a
demonstration; it is deliberately not one of the registered cases a candidate
is accepted on, and the canonical fixtures and validator are untouched by it.

## B1: a third defect, detected but not yet dispatched

S1 and S2 were found by assertions the automation itself wrote. B1 is the
first case found the other way round: the product's own browser console, read
by a scheduled monitor that only looks.

The defect is [apache/superset#44007](https://github.com/apache/superset/issues/44007),
reproduced on the same baseline: a Table column with the `MEMORY_BINARY`
number format shows `1425300509404304697` as raw digits while `4096` in the
same chart renders `4KiB`. The precise severity is a **handled formatter
failure with a visible fallback** — the product logs
`Formatter failed, falling back to raw value TypeError: Cannot convert a
BigInt value to a number` at `warning`, `POST /api/v1/chart/data` returns 200,
there is no pageerror and the chart does not crash. It must not be described
as an exception, a 500 or a crash.

What has actually run, from the monitor container against the isolated
baseline stack, is in [`artifacts/b1-monitor`](../artifacts/b1-monitor):
three read-only scans, two of the defect chart and one of the small-number
control; five events; one incident. Drained under each admission mode, the
same five events produce 0 incidents (`disabled`), 1 recorded and blocked
(`dry_run`) and 1 eligible (`enabled`). The control scan contributes nothing
in any mode, and the second defect scan consolidates into the first incident
rather than proposing a second session.

What has **not** run: no B issue, no repair session, no Slack thread, no
product change. Live dispatch is off pending authorization, so B1 stops at an
eligible incident. Verification for it is written but unexercised against a
real candidate — for a frontend defect it builds the bundle from the
candidate's own commit (the S1/S2 shortcut of reusing the baseline bundle is
invalid here) and refuses a bundle whose provenance is missing or from another
commit.

## What this does not show

- Nothing merged, nothing deployed, no production or public preview.
- Slack is outbound status only. `portal/notify.py` posts lifecycle lines
  through one transport — the Web API client when `SLACK_BOT_TOKEN` is
  configured, the incoming webhook otherwise, never both — into the single
  approved channel `C0C3X4BJ97S`, and de-duplicates by event id. Delivery is
  not exactly-once (ambiguous outcomes are recorded `unknown`, and an
  ambiguous clip upload is neither retried nor counted as delivered). The
  scopes are `chat:write` and `files:write`; nothing reads the channel. There
  is no Q&A: native conversational sync is available only to a session's
  owner — here the `superset-runtime-repair` service user — and is not used,
  and this custom app is not that integration, which is why every message it
  sends is labelled *Superset demo automation*.
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
  preserved unedited; the tests were not entirely offline. One factual
  correction was later posted to the channel
  (`portal.notify correction --text …`, keyed by the correction's own wording
  so it cannot repeat); nothing was edited or deleted. That correction went
  out through the *default* ledger (`runtime/notifications.sqlite`) while the
  three approved messages are recorded in `runtime/live-state`, so its event
  id would not de-duplicate against the other ledger: it must not be sent
  again, and real delivery uses `runtime/live-state` from here on. Neither
  ledger is deleted or rewritten; the historical results are reused as thread
  parents (S2 `1789854458.454909`, S1 `1789854458.664169`) instead of being
  posted a second time — adopted by the explicit `portal.notify bootstrap`
  against `runtime/live-state`, which matches each result to the one stored
  non-simulated repair with that case and accepted head. A fresh state
  inherits no parent and opens its own thread.
- The four authorized final deliveries went out from `runtime/live-state`
  through the bot transport, each under its own historical parent: the S2
  result `ts 1789858187.458229` and clip `F0C2XKS6FD1` under
  `1789854458.454909`, the S1 result `ts 1789858224.753179` and clip
  `F0C33PMRVA6` under `1789854458.664169`, all `sent` at one attempt with no
  ambiguous outcome. Both clips are the 13-second phase 8 replays, described
  as later CLI/API replays of baseline failure versus accepted-head pass —
  not the original phase 6 verification, not portal UI, not a deployment, and
  N1 was not re-run in them. **The S2 result line reads `recording pending —
  not a shareable https link`**: it was sent before the message builder could
  describe a clip published as a file rather than as a URL, so its own clip
  arrived in the thread a minute later without the line naming it. The S1
  line, sent after the fix, reads `recording uploaded to this thread`.
  Nothing in the channel was deleted, and nothing else was edited.
- That S2 line was corrected in place afterwards, once and only there, by
  `portal.notify amend --repair 1 --ts 1789858187.458229` — `chat_update`,
  `sent`, one attempt. The clause "recording pending — not a shareable
  https link" became "recording uploaded to this thread as file
  F0C2XKS6FD1 — S2 captured 2026-09-19T22:15:48+00:00 against
  d234055eaf85, shows later CLI/API replay: baseline failure vs
  accepted-head pass; not phase 6 verification, not portal UI, N1 not
  re-run". Everything else in the message — PR, tested head, verification
  8, `verified in isolated preview; not merged/deployed`, the source label
  — is unchanged, no new message was posted, and the historical parent, the
  correction and the rest of the channel were not touched. It is a status
  correction made after the file was confirmed, not concealment: Slack keeps
  no record of superseded wording, so the previous text, the replacement and
  the reason are stored in the ledger's `notification_amendments` table, and
  the original wording stays quoted above.
- A message can only say a clip was uploaded when the upload ledger holds a
  `sent` row with a file id for that repair and head; supplying
  `--recording thread` on its own now reads `recording prepared for
  attachment in this thread`, because a result is written before its file is
  offered. `amend` refuses a `ts` this ledger never recorded sending, so the
  parent messages and the correction cannot be rewritten through it.
- The S1 case was also captured through the operator portal's own UI, at the
  baseline and at the accepted head, in `artifacts/phase8/portal-ui/`: saved row limit 137 → 1000 on
  `394bca55`, 137 → 137 on `fe266eac`, from a sort-only change. Still a later
  local replay of one case, still not Superset's frontend and not the phase 6
  verification. Only the candidate clip was published, once, into the existing
  S1 thread — `portal.notify attach --repair 2`, file `F0C2NG71TKR`, `sent` at
  one attempt, captured 2026-09-19T22:53:07+00:00 against `fe266eac51a9`. The
  baseline clip stays in the repository only: it runs at `394bca55`, and a file
  under the accepted head's result would read as footage of the fix.
- The fresh run's three messages were re-rendered in place as Block Kit
  cards on 2026-09-20 (`portal.notify restyle`, one `chat.update` each,
  `sent`): the parent `1789859861.249849`, the provisional pull request
  `1789860756.261739` and the preview pass `1789861185.416379`, all in
  `C0C3X4BJ97S`. Presentation only: the lifecycle states, links and
  timestamps are unchanged, the superseded prose is kept verbatim in
  `notification_amendments`, and the before/after payloads were archived
  locally without tokens. The bot token holds no history scope, so Slack's
  own author could not be read (`missing_scope`); ownership rested on the
  operator's read-only confirmation (user `U0C33JQ0Y3U`, bot `B0C2NAG01RD`,
  app `A0C35KD8H8R`), this ledger's record of sending each `ts`, and Slack
  refusing `chat.update` on another app's message. The card copy is derived
  from the stored records — full ids, assertion names, ACU figures and exact
  timestamps stay in the ledger, the reports and this document.
- Two defects, two repairs, one repository, on a fork with a synthetic
  fixture. Nothing here establishes a rate on real customer incidents.
