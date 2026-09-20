# Loom script — one continuous take, target 4:45, hard stop 5:00

One unedited Loom recording, no stitched or pre-recorded clips, no `.mp4`.
The native replay clips and any clip uploaded to `#superset-alerts` are
separate evidence, never part of this take.
Nothing in it costs money: every session, issue and pull request below already
exists, and no repair is dispatched while recording (`AUTO_REPAIR_ENABLED=false`).

When the completed S1 session is opened, say the sentence verbatim:

> **"This repair ran earlier through the API."**

## Before hitting record

| | |
| --- | --- |
| Portal / operator console | <http://127.0.0.1:8092/ops> — private VM loopback, not published; run namespace `fresh-demo-20260919-2310`, dispatch off |
| S1 chart surface | <http://127.0.0.1:8092/settings> — the chart settings page where the sort change is made |
| Native Superset, baseline | <http://127.0.0.1:8088/explore/?slice_id=105> — the same defect in Superset's own Explore UI at `394bca55` |
| Native Superset, candidate | <http://127.0.0.1:8488/explore/?slice_id=105> — PR #6 head `7bb8de7b136f`, an isolated **unmerged preview**, not a deployment |
| Fixture state | Reset the synthetic Table chart to `row_limit` **137** (and `googleCategory10c`) **before** recording, otherwise the read-back shows 1000 → 1000 and the failure does not appear |
| Console login | HTTP basic, operator account; credentials come from the container environment (`docker inspect live-portal-1`), never typed on screen. Log in **before** recording so the browser has the session |
| Baseline Superset | <http://127.0.0.1:8088/health> — should read `OK` |
| Devin session (S1) | <https://app.devin.ai/sessions/e8b63e608d5a4397b114a296420695c7> |
| Pull request (S1) | <https://github.com/woohyeokk-choi/superset/pull/6> @ `7bb8de7b136f9afdc39d31b4c6809b3de467c461` |
| Code tab 1 | `portal/controller.py` — the API create/manage boundary: one durable intent, one session, requested budget and deadline |
| Code tab 2 | `portal/verification.py` — the exact-SHA boundary: the candidate stack built at the PR head and hashed before any replay |
| Stored verdict | the verification report linked from `/ops/incidents/1` |

Open the tabs in this order in advance, log in to each, and dismiss
notifications. The verification's own candidate stack was destroyed after the
attempt, so the verdict is read from the stored report; the stack on 8488 is a
later, separately built preview of the same head — say which one is on screen.

## Screen-by-screen

### 0:00–0:35 · What, and the pain (portal, `/settings`)

Screen: the chart settings page of the synthetic analytics portal, the Table
chart reading back row limit 137.

> "This is a small analytics portal on a fork of Apache Superset. A user
> changes only the sort order of this chart. Watch the row limit."

Change sort. The read-back shows **137 → 1000** — the saved limit is silently
reset. "That is the class of bug I care about: bounded, recurring, reproducible
from a log, and nobody notices until a report is wrong."

### 0:35–1:10 · Logs → incident (`/ops`, then `/ops/incidents/1`)

> "Every request and response is logged server-side, redacted, with a trace
> id. The failing assertion — `row_limit` expected 137, observed 1000 —
> becomes an incident with a fingerprint, so the same failure seen ten times
> is one incident, not ten sessions."

Show the trace, the failing assertion row, then the incident header: family,
fingerprint, baseline SHA.

### 1:10–1:45 · Two code locations

Tab 1 — `portal/controller.py`: the create/manage boundary — a durable intent
row, one session per incident, the requested ACU limit and deadline.
Tab 2 — `portal/verification.py`: the candidate stack built at the exact PR
head and the registered checks. Fifteen seconds each, no line-by-line reading.

> "Those two are the whole design: exactly one session per incident, and a
> verdict nobody but the validator can write."

### 1:45–3:15 · The agent's own work (Devin session tab)

**Say it on opening: "This repair ran earlier through the API."**

Spend the longest single block here, in the session itself, not the dashboard:

- the prompt — the real trace and baseline SHA the controller sent;
- the shell: reproducing the failure on its own checkout;
- the diff it wrote in the Superset product code;
- the tests it ran and its pre-commit output;
- the pull request it opened: [#6](https://github.com/woohyeokk-choi/superset/pull/6).

> "The controller created the fork issue and exactly one session, requested a 20
> ACU limit plus a controller 120-minute deadline, owned by a service user.
> I did not touch the fix."

### 3:15–4:00 · Independent verification (`/ops/incidents/1`, repair section)

> "The session's own report is not the verdict. The coordinator checked out the
> pull request head `7bb8de7b…`, built a candidate stack, hashed the code
> inside the container, and replayed the same scenario — thirteen checks: the
> sort still applies, an omitted row limit keeps 137, an explicit 275 and an
> explicit 1000 are still honoured, and a restricted user still gets 403.
> All held, first attempt, zero follow-ups. That is what
> `verified_in_preview` means here."

Show the stored verification report for the check names (10 for S1, 3 for N1,
all holding) — say plainly that the verification's candidate stack was torn
down and this is the recorded evidence.

### 4:00–4:25 · Why, and the honest limits

> "This isn't 'a chat model can't write this patch'. What is delegated is
> execution and context: reproduce on the right SHA, keep scope bounded, open
> the PR, and be judged by a replay it doesn't control. Neither PR is merged,
> so the baseline is still buggy. No upstream CI ran on either head. Nothing
> is deployed."

### 4:25–4:45 · When I'd use it

> "Start on historical incidents a customer already has, where eligibility and
> permissions are clear, and measure three things before scaling: acceptance
> rate, human touch time, and cost per repair. I measured none of those in two
> runs, so I'm not going to quote a multiplier."

Hard stop by 5:00.

## Do not say

- That Slack alerted the channel during either live repair: both channel
  summaries were sent afterwards from stored records, marked *Historical
  result — repair ran earlier*.
- That Slack Q&A works, or that either repair alerted the channel while it
  ran — the notifier is outbound status only, and the two messages in
  `#superset-alerts` are backfills marked "Historical result". The alert app
  is a custom one labelled *Superset demo automation*, not the official Devin
  integration.
- That the replay clips show the product's UI. They are scenario output plus
  the persisted REST read-back; the pinned light image serves no compiled
  Superset frontend, and no clip of the custom portal's own screens at an
  accepted head exists yet.
- That the fresh run is finished. PR #6 is open and unmerged: the symptom
  clips and one labelled candidate **preview** clip are delivered, and the
  after-merge clip cannot be recorded until a human merges and the deployment
  is rebuilt at GitHub's merge commit.
- That the candidate stack on 8488 is a deployment. It is an isolated
  loopback preview of an unmerged head on the builder machine.
- That the native Explore frames in
  `artifacts/fresh-demo-20260919-2310/visual-preview/` show the fix or a
  deployment. They are the baseline, on the builder's isolated loopback stack
  — a different machine from the repair session's VM — and the presentation
  chart they use is a labelled demonstration, not a registered case.
- That CI passed. Both candidate heads had 0 check-runs and 0 statuses.
- Any dollar figure derived from `acus_consumed`, or any ROI multiplier.
- That a preview environment is still running.
