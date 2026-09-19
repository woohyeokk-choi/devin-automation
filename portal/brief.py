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

from .handoff import SETUP, automation_pin, bundle_events
from .handoff import reproduction_markdown

LABELS = ["runtime-repair"]
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


def marker(incident: dict[str, Any], attempt: int) -> str:
    """Durable identity for one dispatch attempt, echoed into the remote object.

    The controller writes this before it calls anything. After a crash or an
    ambiguous timeout it searches for this string instead of creating a second
    issue or a second session.
    """
    return f"runtime-repair:{incident['fingerprint']}:attempt-{attempt}"


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


def issue_body(incident: dict[str, Any], attempt: int, versions: dict[str, Any]) -> str:
    events, gaps = bundle_events(incident)
    return f"""{reproduction_markdown(incident, events, gaps, versions)}

## Contract

| Assertion | Expected | Observed |
|---|---|---|
{_contract(incident)}

<!-- {marker(incident, attempt)} -->
"""


def issue_title(incident: dict[str, Any]) -> str:
    return f"[runtime-repair] {incident['title']}"


def prompt(incident: dict[str, Any], attempt: int, versions: dict[str, Any], issue_url: str) -> str:
    events, gaps = bundle_events(incident)
    automation = automation_pin(versions)
    evidence = [
        {
            k: v
            for k, v in event.items()
            if k in ("ts_utc", "trace_id", "step_index", "operation", "outcome", "assertion")
        }
        for event in events
    ]
    return f"""A user action in a synthetic-data analytics portal fails against
Apache Superset at a fixed baseline commit. Reproduce it, fix the product
code, and open a pull request.

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

| Assertion | Expected | Observed |
|---|---|---|
{_contract(incident)}

{TASK.format(repo=incident['target_repo'], base=BASE_BRANCH)}

{GUARDRAILS.format(repo=incident['target_repo'], base=BASE_BRANCH)}

## Rebuild the environment

{SETUP.format(
    repo=incident['target_repo'],
    sha=incident['baseline_sha'],
    fixture=incident['fixture_revision'],
    automation_repo=versions.get('automation_repo', 'woohyeokk-choi/devin-automation'),
    automation_sha=automation['sha'],
    automation_note=automation['note'],
)}

## Observed sequence

Sanitized server-side evidence for the failed user actions — allowlisted
fields only, no credentials, cookies, tokens, raw headers or raw bodies:

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
) -> dict[str, Any]:
    """The exact v3 create-session body, recorded whether or not it is sent."""
    return {
        "prompt": prompt(incident, attempt, versions, issue_url),
        "title": f"[runtime-repair] {incident['title']}",
        "repos": [incident["target_repo"]],
        "tags": ["runtime-repair", incident["fingerprint"], marker(incident, attempt)],
        "max_acu_limit": acu_limit,
        "structured_output_required": True,
        "structured_output_schema": OUTPUT_SCHEMA,
        "resumable": True,
    }


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
