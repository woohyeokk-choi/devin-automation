# Loom script — one continuous take, target 4:50, hard stop 5:00

One unedited Loom recording: one continuous screen take, no stitched or
pre-recorded segments cut into it. Briefly *playing* an already-delivered
evidence clip on screen inside that continuous take is fine and is part of the
walkthrough; the deliverable is the Loom link, not the standalone `.mp4`
files.

Nothing in it costs money: every session, issue and pull request below already
exists, and no repair is dispatched while recording (`AUTO_REPAIR_ENABLED=false`).
Nothing is re-run either: the walkthrough is **chart-first and read-only**. Do
not reset the historical fixture and do not change a chart's sort while
filming — that would write a new incident over the case being narrated.

When the completed S1 session is opened, say the sentence verbatim:

> **"This repair ran earlier through the API."**

## Before hitting record

| | |
| --- | --- |
| Native Superset, baseline | <http://127.0.0.1:8088/explore/?slice_id=9> — `Order revenue - 10-row view (presentation)` at `394bca55`, already in its reset state: saved row limit **1000**, ~600 rows on screen |
| Native Superset, candidate | <http://127.0.0.1:8488/explore/?slice_id=105> — same chart at PR #6 head `7bb8de7b136f`: saved row limit **10**, 10 rows, sorted descending. An isolated **unmerged preview**, not a deployment |
| Portal / operator console | <http://127.0.0.1:8092/ops> — private VM loopback, not published; run namespace `fresh-demo-20260919-2310`, dispatch off |
| Stored incident | <http://127.0.0.1:8092/ops/incidents/1> — the behaviour-check failure, the repair timeline and the verification report |
| Slack thread | `#superset-alerts`, parent `1789859861.249849` — three status posts from the run plus three clips |
| Console login | HTTP basic, operator account; credentials come from the container environment, never typed on screen. Log in **before** recording so the browser has the session |
| Devin session (S1) | <https://app.devin.ai/sessions/e8b63e608d5a4397b114a296420695c7> |
| Pull request (S1) | <https://github.com/woohyeokk-choi/superset/pull/6> @ `7bb8de7b136f9afdc39d31b4c6809b3de467c461` |
| Code tab 1 | [`portal/controller.py` L762–798](https://github.com/woohyeokk-choi/devin-automation/blob/369c153fdc48c6289da772a55b6d52819a77c6ff/portal/controller.py#L762-L798) — the API create/manage boundary: one durable intent, one session, requested budget and deadline |
| Code tab 2 | [`portal/verification.py` L404–481](https://github.com/woohyeokk-choi/devin-automation/blob/369c153fdc48c6289da772a55b6d52819a77c6ff/portal/verification.py#L404-L481) — the exact-SHA boundary: the candidate stack built at the PR head and hashed before any replay |
| Product diff | [`chart_utils.py` L896–903](https://github.com/woohyeokk-choi/superset/blob/7bb8de7b136f9afdc39d31b4c6809b3de467c461/superset/mcp_service/chart/chart_utils.py#L896-L903) and [`plugins/table.py` L131–133](https://github.com/woohyeokk-choi/superset/blob/7bb8de7b136f9afdc39d31b4c6809b3de467c461/superset/mcp_service/chart/plugins/table.py#L131-L133), with [three regression tests](https://github.com/woohyeokk-choi/superset/blob/7bb8de7b136f9afdc39d31b4c6809b3de467c461/tests/unit_tests/mcp_service/chart/tool/test_update_chart.py#L146-L240) — the whole repair, 103 added lines |

Open the tabs in this order in advance, log in to each, and dismiss
notifications. Both loopback stacks are temporary: check the two charts load
shortly before recording and say they were *checked just before this take*,
not that anything is permanently up. The verification's own candidate stack
was destroyed after the attempt, so the verdict is read from the stored
report; the stack on 8488 is a later, separately built preview of the same
head — say which one is on screen.

Two things that look alike and are not the same run: the original incident is
the registered behaviour check that saw **137 → 1000**, and the presentation
chart used for the native footage is a later, separate local replay of the
same defect (**10 → 1000**, ~600 rows). Never merge their timestamps into one
story.

## Screen-by-screen

### 0:00–0:30 · The symptom, in Superset's own UI (8088, slice 9)

Screen: native Explore on the baseline. The chart is a raw-record table whose
saved row limit reads **1000** with roughly 600 rows rendered, although it was
saved as a 10-row view.

> "A sort-only update through Superset's MCP API omitted the row limit. The
> saved limit didn't stay — it was replaced by the schema default. This is the
> state that update left behind; I'm not re-running it now."

### 0:30–1:05 · The stored event, not a story (`/ops`, then `/ops/incidents/1`)

> "Every request and response is logged server-side, redacted, with a trace
> id. The failing behaviour check — `row_limit` expected 137, observed 1000 —
> became one incident with a fingerprint, so the same failure seen ten times
> is one incident, not ten sessions."

Show the trace, the failing assertion row, then the incident header: family
`omitted_row_limit_is_reset`, fingerprint, baseline SHA.

### 1:05–1:30 · Part 3: what the operator actually sees

Stay on `/ops/incidents` and read the run summary card:

> "One distinct incident, one repair, one linked pull request, one independent
> preview pass, zero merge-verified, zero needing attention. Twenty ACU
> requested; consumption reported by the API is **unknown**, and I show it as
> unknown rather than as zero. The last recorded change is timestamped, and
> because the work is still open and older than half an hour it says *no fresh
> poll* — that is a freshness statement about stored records, not a claim
> about what any process is doing."

### 1:30–2:40 · The agent's own work (Devin session tab)

**Say it on opening: "This repair ran earlier through the API."**

The longest single block, in the session itself, not the dashboard:

- the prompt — the real trace and baseline SHA the controller sent;
- the shell: reproducing the failure on its own checkout;
- the diff it wrote in the Superset product code;
- the tests it ran and its pre-commit output;
- the pull request it opened: [#6](https://github.com/woohyeokk-choi/superset/pull/6).

> "The controller created the fork issue and exactly one session, requested a 20
> ACU limit plus a controller 120-minute deadline, owned by a service user.
> I did not touch the fix."

### 2:40–3:05 · Two code locations

Tab 1 — `portal/controller.py`: the create/manage boundary — a durable intent
row, one session per incident, the requested ACU limit and deadline.
Tab 2 — `portal/verification.py`: the candidate stack built at the exact PR
head and hashed before a single check runs. Twelve seconds each, no
line-by-line reading.

> "Those two are the whole design: exactly one session per incident, and a
> verdict nobody but the validator can write."

Optionally show the product diff: three files, 103 added lines, no deletions.

### 3:05–3:40 · Independent verification (`/ops/incidents/1`, repair section)

> "The session's own report is not the verdict. The coordinator checked out the
> pull request head `7bb8de7b…`, built a candidate stack, hashed the code
> inside the container, and replayed the same scenario — thirteen checks: the
> sort still applies, an omitted row limit keeps the saved value, an explicit
> 275 and an explicit 1000 are still honoured, and a restricted user still
> gets 403. All held, first attempt, zero follow-ups. That is what
> `verified_in_preview` means here."

Show the stored report for the check names (10 for S1, 3 for N1, all holding)
— say plainly that the verification's candidate stack was torn down and this
is the recorded evidence.

### 3:40–4:10 · The candidate chart, and the channel (8488 slice 105, then Slack)

Switch to the candidate tab: the same chart at the PR head, saved limit 10, 10
rows, sorted descending — the requested sort applied and the saved limit kept.

> "Unmerged preview on a loopback stack, checked just before this recording.
> Not a deployment."

Then the Slack thread: the three status posts the coordinator sent during the
run — investigating, pull request, verified-awaiting-merge — and the three
clips. Play a few seconds of the candidate clip if it helps; its caption says
*unmerged PR #6, local replay, verified in isolated preview; not
merged/deployed*.

### 4:10–4:35 · Why, and the honest limits

> "This isn't 'a chat model can't write this patch'. What is delegated is
> execution and context: reproduce on the right SHA, keep scope bounded, open
> the PR, and be judged by a replay it doesn't control. The pull request is
> not merged, so the baseline is still buggy. No upstream CI ran on the head.
> Nothing is deployed, and the after-merge video does not exist yet."

### 4:35–4:50 · When I'd use it

> "Start on historical incidents a customer already has, where eligibility and
> permissions are clear, and measure three things before scaling: acceptance
> rate, human touch time, and cost per repair. I measured none of those here,
> so I'm not going to quote a multiplier."

Hard stop by 5:00.

## Do not say

- That the fresh run's Slack posts were written afterwards. In
  `fresh-demo-20260919-2310` the coordinator posted each transition as it
  happened (investigation 23:17:41Z, ~70 seconds after the failure; pull
  request 23:32:35Z; verified-awaiting-merge 23:39:45Z) and re-rendered them
  as Block Kit cards at 23:58Z. It is the **older S2/S1 runs** whose two
  messages are backfills marked *Historical result — repair ran earlier*; do
  not apply that label to this run, and do not call any of it paging or
  on-call alerting.
- That Slack Q&A works — the notifier is outbound status only, and the app is
  a custom one labelled *Superset demo automation*, not the official Devin
  integration.
- That the earlier scenario replay clips show the product's UI. Those are
  scenario output plus the persisted REST read-back. The two native clips are
  different: `F0C2XU6V475` is Superset's own Explore on the baseline, and
  `F0C36VA3HQA` is Superset's own Explore at candidate `7bb8de7b136f`, both
  recorded locally on this machine — neither is a deployment or a post-merge
  recording.
- That the fresh run is finished. PR #6 is open and unmerged: the after-merge
  clip cannot be recorded until a human merges and the deployment is rebuilt
  at GitHub's merge commit.
- That the candidate stack on 8488 is a deployment, or that any preview
  environment is permanently up. It is an isolated loopback preview of an
  unmerged head on the builder machine, true as of the moment it was checked.
- That the native Explore frames in
  `artifacts/fresh-demo-20260919-2310/visual-preview/` show the fix. They are
  the baseline, on the builder's isolated loopback stack — a different machine
  from the repair session's VM — and the presentation chart they use is a
  labelled demonstration, not a registered case.
- That CI passed. Both candidate heads had 0 check-runs and 0 statuses.
- Any dollar figure derived from `acus_consumed`, or any ROI multiplier.
