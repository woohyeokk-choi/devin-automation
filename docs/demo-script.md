# Loom script — one continuous take, target 4:45, hard stop 5:00

One unedited Loom recording, no stitched or pre-recorded clips, no `.mp4`.
Nothing in it costs money: every session, issue and pull request below already
exists, and no repair is dispatched while recording (`AUTO_REPAIR_ENABLED=false`).

When the completed S1 session is opened, say the sentence verbatim:

> **"This repair ran earlier through the API."**

## Before hitting record

| | |
| --- | --- |
| Portal / operator console | <http://127.0.0.1:8091/ops> — private VM loopback, not published |
| Console login | HTTP basic, operator account; credentials come from the container environment (`docker inspect live-portal-1`), never typed on screen. Log in **before** recording so the browser has the session |
| Baseline Superset | <http://127.0.0.1:8088/health> — should read `OK` |
| Devin session (S1) | <https://app.devin.ai/sessions/18b04127f4a44af6a9c71f9eb3eaba9e> |
| Pull request (S1) | <https://github.com/woohyeokk-choi/superset/pull/4> @ `fe266eac51a996760a75997ff94b3270c9ef73b1` |
| Code tab 1 | `portal/incidents.py` — fingerprint + dedup |
| Code tab 2 | `portal/verification.py` — replay at the exact PR head |
| Stored verdict | `artifacts/phase6/S1/verification-passed.json` |

Open all seven tabs in this order in advance, log in to each, and dismiss
notifications. Candidate stacks were destroyed after verification, so the
verdict is read from the stored artifact — do not imply a live preview.

## Screen-by-screen

### 0:00–0:35 · What, and the pain (portal, `/` chart page)

Screen: the synthetic analytics portal, the Table chart with row limit 137.

> "This is a small analytics portal on a fork of Apache Superset. A user
> changes only the sort order of this chart. Watch the row limit."

Change sort. The read-back shows **137 → 1000** — the saved limit is silently
reset. "That is the class of bug I care about: bounded, recurring, reproducible
from a log, and nobody notices until a report is wrong."

### 0:35–1:10 · Logs → incident (`/ops`, then `/ops/incidents/2`)

> "Every request and response is logged server-side, redacted, with a trace
> id. The failing assertion — `row_limit` expected 137, observed 1000 —
> becomes an incident with a fingerprint, so the same failure seen ten times
> is one incident, not ten sessions."

Show the trace, the failing assertion row, then the incident header: family,
fingerprint, baseline SHA.

### 1:10–1:45 · Two code locations

Tab 1 — `portal/incidents.py`: the fingerprint and the dedup insert.
Tab 2 — `portal/verification.py`: the candidate stack built at the PR head and
the registered checks. Fifteen seconds each, no line-by-line reading.

> "Those two are the whole design: one incident per failure family, and a
> verdict nobody but the validator can write."

### 1:45–3:15 · The agent's own work (Devin session tab)

**Say it on opening: "This repair ran earlier through the API."**

Spend the longest single block here, in the session itself, not the dashboard:

- the prompt — the real trace and baseline SHA the controller sent;
- the shell: reproducing the failure on its own checkout;
- the diff it wrote in the Superset product code;
- the tests it ran and its pre-commit output;
- the pull request it opened: [#4](https://github.com/woohyeokk-choi/superset/pull/4).

> "The controller created the fork issue and exactly one session, capped at 20
> ACUs and 120 minutes, owned by a service user. I did not touch the fix."

### 3:15–4:00 · Independent verification (`/ops/incidents/2`, repair section)

> "The session's own report is not the verdict. The coordinator checked out the
> pull request head `fe266eac…`, built a candidate stack, hashed the code
> inside the container, and replayed the same scenario — thirteen checks: the
> sort still applies, an omitted row limit keeps 137, an explicit 275 and an
> explicit 1000 are still honoured, and a restricted user still gets 403.
> All held, first attempt, zero follow-ups. That is what
> `verified_in_preview` means here."

Show the stored `verification-passed.json` for the check names — say plainly
that the candidate stack was torn down and this is the recorded evidence.

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

- That Slack Q&A works, or that either repair alerted the channel while it
  ran — the notifier is outbound status only, and the two messages in
  `#superset-alerts` are backfills marked "Historical result".
- That CI passed. Both candidate heads had 0 check-runs and 0 statuses.
- Any dollar figure derived from `acus_consumed`, or any ROI multiplier.
- That a preview environment is still running.
