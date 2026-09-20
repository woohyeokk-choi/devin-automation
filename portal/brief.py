"""The text the controller would send: the fork issue and the Devin prompt.

Both are built from the same stored, sanitized evidence as the handoff bundle
and are self-contained on purpose. A prompt that only points at
`http://127.0.0.1:8090/ops/incidents/3` hands over nothing: that host is not
reachable from the session, so the incident JSON, the contract values, the
reproduction steps and the bootstrap are embedded inline.
"""

from __future__ import annotations

import json
from typing import Any

from .handoff import SETUP, STEPS, automation_pin, bundle_events
from .handoff import reproduction_markdown

#: The allowlisted view of one stored event that goes into the prompt. It is
#: the request/response summary, not only the assertion: a session asked to
#: reproduce a sequence needs the calls and their outcomes.
PROMPT_FIELDS = (
    "ts_utc",
    "trace_id",
    "request_id",
    "step_index",
    "operation",
    "tool_name",
    "http_status",
    "outcome",
    "input",
    "output",
    "assertion",
)

LABELS = ["runtime-repair"]

#: The protected commit every repair starts from. A demo run may integrate
#: into a branch of its own, but the default stays the branch the historical
#: repairs targeted: a deployment that configures nothing keeps behaving as
#: it did.
BASE_BRANCH = "runtime-repair/baseline"

#: Everything the repair session is not allowed to do. Listed in the prompt
#: because the session has write access to the fork and the automation repo is
#: the thing judging it: a session that "fixes" the validator has fixed nothing.
GUARDRAILS = """## Constraints

- Open the pull request **only** in `{repo}`, against the `{base}` branch.
- Do not modify the automation repository's validator, scenarios or
  assertions; they are the independent check on your work.
- Do not weaken or disable authentication, authorization or CSRF anywhere.
- Do not push to `master`, do not merge, do not deploy, and do not open
  anything against `apache/superset`.
- Do not change the synthetic fixture to make an assertion pass.
- Stay inside the ACU limit configured on this session. If you need more
  budget, say so and stop; do not work around the limit."""

TASK = """## What to do, in order

1. **Reconstruct the environment** from the bootstrap block below and confirm
   it is the baseline SHA named above.
2. **Reproduce the failure yourself** before changing anything, using the
   reproduction steps. If you cannot reproduce it, stop and report that —
   a fix for a failure you never observed is not acceptable.
3. **Classify it**: product code defect, configuration/environment problem,
   or correct behaviour (for example an expected permission denial). Only
   the first warrants a code change; report the others and stop.
4. **Fix the product code** in the smallest way that addresses the cause.
5. **Add a focused regression test** in the product repository that fails
   before your change and passes after it.
6. **Open one pull request** in `{repo}` against `{base}`.

Report through structured output when you finish."""

#: What the controller needs back from the session in machine-readable form.
#: `pr_url` is validated against the allowed host/repo/base and its head SHA is
#: read from GitHub — the agent's word is an input, not a verdict.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reproduced": {
            "type": "boolean",
            "description": "Did you observe the reported failure yourself before fixing it?",
        },
        "reproduction_evidence": {
            "type": "string",
            "description": "Commands run and the observed values, before any change.",
        },
        "classification": {
            "type": "string",
            "enum": ["product_defect", "configuration", "environment", "expected_behaviour"],
        },
        "pr_url": {"type": "string", "description": "Pull request URL, or empty if none."},
        "regression_test": {
            "type": "string",
            "description": "Path::name of the test that fails before the fix.",
        },
        "summary": {"type": "string"},
    },
    "required": ["reproduced", "classification", "summary"],
}


def marker(incident: dict[str, Any], attempt: int, run: str = "") -> str:
    """Durable identity for one dispatch attempt, echoed into the remote object.

    The controller writes this before it calls anything. After a crash or an
    ambiguous timeout it searches for this string instead of creating a second
    issue or a second session.

    A run namespace separates one deliberate re-run of a known incident from
    the original: the fingerprint is the same failure and must stay the same,
    so without a namespace this attempt would reconcile onto the issue and
    session the first run created. Production leaves it empty.
    """
    scope = f"{run}:" if run else ""
    return f"runtime-repair:{scope}{incident['fingerprint']}:attempt-{attempt}"


#: What "fixed" means for a formatting defect, written before the fix exists
#: so the session is not left to invent its own acceptance bar. The precision
#: language is deliberate: a memory unit display is *supposed* to round, so
#: two nearby inputs may legitimately print the same string at the requested
#: precision. What may not happen is a value losing its meaning.
ACCEPTANCE = """## What counts as fixed

- The chosen format is applied to values outside the JavaScript safe-integer
  range, and the product stops logging the formatter warning for them.
- The raw numeric meaning is preserved. A rounded human-readable unit display
  may render two nearby inputs identically at the requested precision — that
  is what rounding is — but a value must never be silently turned into a
  different number.
- Converting values to a JavaScript `Number` wherever they appear is **not**
  an acceptable fix: that conversion is the precision loss this defect is
  made of.
- The control keeps working: values inside the safe range format exactly as
  before, in the same chart, with the same format.
- Keep the change narrow and led by the trace. Do not rework unrelated
  formatter families (currency, percent, time) unless your own reproduction
  shows the same code path is responsible."""


def _telemetry_rows(incident: dict[str, Any]) -> str:
    """The browser's own words, for an incident admitted from telemetry.

    A telemetry incident has no expected/observed assertion pair — inventing
    one would misrepresent how it was detected — so the brief carries the
    captured severity, diagnostic and what the chart was showing instead.
    """
    rows = set()
    for event in incident["events"]:
        body = event.get("output") or {}
        if event.get("outcome") != "telemetry" or not isinstance(body, dict):
            continue
        page = body.get("page") or {}
        rows.add(
            f"| `{body.get('severity')}` | `{body.get('diagnostic')}` | "
            f"{body.get('occurrences_in_scan')} | `{page.get('visible_values')}` |"
        )
    if not rows:
        return ""
    header = (
        "\n## Observed browser evidence\n\n"
        "Captured by a scheduled read-only scan of the saved chart, not by an\n"
        "assertion: the severity below is the one the product emitted.\n\n"
        "| Severity | Diagnostic | Occurrences in one scan | Values on screen |\n"
        "|---|---|---|---|\n"
    )
    return header + "\n".join(sorted(rows)) + "\n"


def _contract(incident: dict[str, Any]) -> str:
    rows = {
        (
            (e["assertion"]).get("name", ""),
            json.dumps((e["assertion"]).get("expected")),
            json.dumps((e["assertion"]).get("observed")),
        )
        for e in incident["events"]
        if e.get("assertion") and not (e["assertion"]).get("holds")
    }
    return "\n".join(
        f"| `{name}` | `{expected}` | `{observed}` |" for name, expected, observed in sorted(rows)
    )


def issue_body(
    incident: dict[str, Any], attempt: int, versions: dict[str, Any], run: str = ""
) -> str:
    events, gaps = bundle_events(incident)
    return f"""{_rerun_note(run)}{reproduction_markdown(incident, events, gaps, versions)}
{_evidence_section(incident)}
{ACCEPTANCE}

<!-- {marker(incident, attempt, run)} -->
"""


def _evidence_section(incident: dict[str, Any]) -> str:
    """Whichever of the two detection paths actually produced this incident."""
    contract = _contract(incident)
    if not contract:
        return _telemetry_rows(incident)
    return f"""
## Contract

| Assertion | Expected | Observed |
|---|---|---|
{contract}
"""


def _rerun_note(run: str) -> str:
    """Say, in the object itself, that a run is a repeat of a known failure."""
    if not run:
        return ""
    return (
        f"> Demonstration run `{run}`. This failure was diagnosed and repaired "
        "once before, so this is a rehearsal of the pipeline on a known "
        "defect, not a newly discovered one. The evidence below was captured "
        "by this run.\n\n"
    )


def _prompt_rerun_note(run: str) -> str:
    if not run:
        return ""
    return (
        f"\nThis is demonstration run `{run}` of an incident that was "
        "diagnosed and repaired once before: a known defect, replayed to "
        "exercise the pipeline. Repository context about the earlier repair "
        "may exist and you may read it, but reproduce the failure yourself "
        "from the evidence below before changing anything, and say in your "
        "report what you observed rather than what a previous fix did.\n"
    )


def issue_title(incident: dict[str, Any], run: str = "") -> str:
    scope = f"[{run}]" if run else ""
    return f"[runtime-repair]{scope} {incident['title']}"


def prompt(
    incident: dict[str, Any],
    attempt: int,
    versions: dict[str, Any],
    issue_url: str,
    run: str = "",
    base: str = BASE_BRANCH,
) -> str:
    events, gaps = bundle_events(incident)
    automation = automation_pin(versions)
    evidence = [
        {k: v for k, v in event.items() if k in PROMPT_FIELDS} for event in events
    ]
    return f"""A user action in a synthetic-data analytics portal fails against
Apache Superset at a fixed baseline commit. Reproduce it, fix the product
code, and open a pull request.
{_prompt_rerun_note(run)}
{('Tracking issue: ' + issue_url) if issue_url else 'No tracking issue was created.'}

## Incident

```json
{json.dumps(
    {
        "fingerprint": incident["fingerprint"],
        "family": incident["family"],
        "scenario": incident["scenario"],
        "title": incident["title"],
        "target_repo": incident["target_repo"],
        "baseline_sha": incident["baseline_sha"],
        "fixture_revision": incident["fixture_revision"],
        "actor": incident["actor"],
        "failed_user_actions": incident["occurrence_count"],
        "first_seen_at": incident["first_seen_at"],
        "last_seen_at": incident["last_seen_at"],
    },
    indent=2,
)}
```

{_evidence_section(incident)}
{TASK.format(repo=incident['target_repo'], base=base)}

{ACCEPTANCE}

{GUARDRAILS.format(repo=incident['target_repo'], base=base)}

## Rebuild the environment

{SETUP.format(
    repo=incident['target_repo'],
    sha=incident['baseline_sha'],
    fixture=incident['fixture_revision'],
    automation_repo=versions.get('automation_repo', 'woohyeokk-choi/devin-automation'),
    automation_sha=automation['sha'],
    automation_note=automation['note'],
)}

## Reproduce this case

{STEPS.get(incident['scenario'], 'No scripted steps are registered for this scenario.')}

## Observed sequence

Sanitized server-side evidence for the failed user actions — every stored
upstream REST/MCP call with its allowlisted input, output and HTTP status,
not only the assertions, and no credentials, cookies, tokens, raw headers or
raw bodies:

```json
{json.dumps(evidence, indent=2)}
```

{('Evidence gaps: ' + '; '.join(gaps)) if gaps else 'No evidence gaps: every trace behind this incident is included.'}
"""


def session_request(
    incident: dict[str, Any],
    attempt: int,
    versions: dict[str, Any],
    issue_url: str,
    *,
    acu_limit: int,
    run: str = "",
    base: str = BASE_BRANCH,
) -> dict[str, Any]:
    """The exact v3 create-session body, recorded whether or not it is sent."""
    return {
        "prompt": prompt(incident, attempt, versions, issue_url, run, base),
        "title": issue_title(incident, run),
        "repos": [incident["target_repo"]],
        "tags": [
            "runtime-repair",
            *([run] if run else []),
            incident["fingerprint"],
            marker(incident, attempt, run),
        ],
        "max_acu_limit": acu_limit,
        "structured_output_required": True,
        "structured_output_schema": OUTPUT_SCHEMA,
        "resumable": True,
    }


def post_merge_message(
    merge_sha: str, base: str, pr_url: str, head_sha: str, case: str = "S1"
) -> str:
    """Ask the session that wrote the fix to record the merged code.

    The file name carries the metadata, because the host reads the capture
    back through the attachments API and has to know what it is looking at
    without trusting a sentence: case, full commit id and capture time are
    all in the name, and a file that does not carry them is not accepted.
    """
    return f"""A human merged your pull request and independent host verification
has already replayed the scenario against the merged commit and accepted it.

Pull request: {pr_url} (previewed head `{head_sha}`)
Merge commit: `{merge_sha}` on `{base}`

One last piece of work, inside your existing budget: build that exact merge
commit and record the repaired behaviour.

The recording must show Superset's own chart, not only our portal screen. A
customer has to see the table they use:

1. Build the frontend from this same merge commit's `superset-frontend` tree
   (`docker build --target superset-node` is the supported path) and serve
   those assets, so Explore actually renders.
2. Create the chart to record on your own machine; do not go looking for a
   chart id from somewhere else, and do not use the aggregate chart the
   incident was reported on — its rendered ordering is decided by a separate
   mapping that this fix does not touch. Make a new table chart on the
   `synthetic_orders` dataset with `query_mode` set to `raw`, the columns
   `id`, `region`, `channel`, `product` and `revenue`, a row limit of 10 and
   `revenue` ordered ascending. Leave the reported chart, the synthetic
   fixture and the automation repository's checks alone.
3. Open that chart in Superset's native Explore UI with the row limit control
   visible, and show the starting state: the saved row limit of 10 and the
   number of rows the table renders.
4. Perform the same sort-only user action on it — order `revenue` descending
   instead, and send no row limit at all.
5. Reload the native chart and show three things on screen: the saved row
   limit is still 10, the rendered row count is unchanged, and the ordering
   you asked for is the one displayed. Read the counts off Superset's own row
   badge rather than describing them.

Then attach it as a single video named exactly

    post-merge-{merge_sha}-<YYYYMMDDTHHMMSSZ>-{case}.mp4

where the timestamp is the UTC time you captured it. The name is how the
recording is identified, so a file named anything else is ignored. Reply with
the full commit id you built, that capture time, the row limits, row counts
and orderings you observed, and the environment the recording shows — it is
your own machine, not the verifier's, and the two have different loopback
addresses. Do not change product code, open another pull request, merge or
deploy anything. The recording is evidence of the merged code, not a new
verdict: the host checks remain the source of truth."""


def symptom_message(baseline_sha: str, case: str = "S1") -> str:
    """Ask the session to keep footage of the failure it is reproducing.

    Reproduction is already the first thing the session was briefed to do,
    so this buys no extra work: it asks for the reproduction it is running
    anyway to be recorded before any product code changes. Nobody's browser
    can be recorded retrospectively, which is why the request goes out with
    the dispatch rather than after a fix exists.
    """
    return f"""While you reproduce this on the unchanged baseline, record that
reproduction \u2014 before you change any product code.

Baseline commit: `{baseline_sha}`

Capture the failing user action through the portal UI and attach it as a single
video named exactly

    symptom-{baseline_sha}-<YYYYMMDDTHHMMSSZ>-{case}.mp4

where the timestamp is the UTC time you captured it. The name is how the
recording is identified, so a file named anything else is ignored, and a clip
of anything other than the baseline failing is not this evidence. Keep
credentials and any real data out of the frame.

If you have already moved past reproduction, do not undo your work: check the
baseline commit out separately and replay the same action there. The clip is
published as a replay recorded after detection either way, never as the
original browser session. If you cannot record at all, say so plainly in a
reply rather than attaching something else; a missing recording is recorded as
missing.

Then carry on with the repair exactly as briefed: this recording is evidence
of the symptom, not a verdict, and the host's checks remain the source of
truth."""


def follow_up_message(failures: list[str], pr_url: str, head_sha: str) -> str:
    """Verification feedback, delivered to the same session that produced the PR."""
    bullets = "\n".join(f"- {failure}" for failure in failures)
    return f"""Independent verification replayed the same scenario against your
pull request and it still fails.

Pull request: {pr_url} (head `{head_sha}`)

Failed behavioural checks:
{bullets}

Reproduce these against your own branch, correct the fix and push to the same
pull request. Do not open a second pull request, and do not change the
automation repository's assertions."""
