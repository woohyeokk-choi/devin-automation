#!/usr/bin/env python3
"""Publish a stored verification record as reviewable evidence.

The record is nested deeper than the event redactor is willing to walk:
:func:`portal.redaction.scrub` stops at depth 6 and 25 items so that a
hostile trace cannot exhaust the logger, which is right there and wrong
here — it replaces exactly the check names and observed values the export
exists to show. So this walks the tree itself and sanitises the leaves,
keeping booleans and numbers as they are. Nothing is reconstructed: every
value is copied from the stored report and the stored row.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.redaction import scrub_text  # noqa: E402
from portal.verification import grade  # noqa: E402

PASSED = "passed"


def sanitize(value: Any) -> Any:
    """Redact credential-shaped text at every depth, preserving structure."""
    if isinstance(value, dict):
        return {str(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        return scrub_text(value)
    return scrub_text(str(value))


def export(live: Path, out: Path, verification_id: int, repair_id: int) -> Path:
    verifications = sqlite3.connect(live / "verifications.sqlite")
    verifications.row_factory = sqlite3.Row
    repairs = sqlite3.connect(live / "repairs.sqlite")
    repairs.row_factory = sqlite3.Row
    record = dict(
        verifications.execute(
            "SELECT * FROM verifications WHERE id = ?", (verification_id,)
        ).fetchone()
    )
    repair = dict(
        repairs.execute("SELECT * FROM repairs WHERE id = ?", (repair_id,)).fetchone()
    )
    report = json.loads(Path(record["artifact_path"]).read_text())
    cases = tuple(json.loads(record["cases"]))

    verdict, reasons = grade(report, cases)
    if verdict != PASSED or reasons:
        raise SystemExit(f"stored report does not grade as passed: {verdict} {reasons}")

    document = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "independent verification of a live repair candidate",
        "simulated": bool(record["simulated"]),
        "incident_id": record["incident_id"],
        "repair": {
            "repair_id": repair["id"],
            "state": repair["state"],
            "attempt": repair["attempt"],
            "product_follow_ups_sent": repair["follow_ups"],
            "terminal_reason": repair["terminal_reason"],
            "issue_url": repair["issue_url"],
            "session_url": repair["session_url"],
            "pr_url": repair["agent_pr_url"],
            "pr_head_sha": repair["pr_head_sha"],
            "requested_acu_limit": repair["acu_limit"],
            "deadline_utc": repair["deadline_utc"],
            "api_reported_usage": {
                "field": "acus_consumed",
                "value": repair["agent_acus"],
                "note": (
                    "as the Devin v3 session read returned it at the final poll. "
                    "Session reads carry no max_acu_limit field, so the requested "
                    "cap is only what the create body asked for, and ACUs are not "
                    "dollars."
                ),
            },
            "agent_self_report": {
                "status": repair["agent_status"],
                "status_detail": repair["agent_detail"],
                "note": "the agent's own readiness claim; it did not decide the verdict",
            },
        },
        "verification": {
            "verification_id": record["id"],
            "verdict": record["verdict"],
            "graded_verdict": verdict,
            "graded_note": (
                "portal.verification.grade() rebuilt this verdict from the checks "
                "below, ignoring the report's own summary"
            ),
            "cases": list(cases),
            "candidate_sha": record["candidate_sha"],
            "pr_url": record["pr_url"],
            "validator_ref": record["validator_ref"],
            "fixture_rev": record["fixture_rev"],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "commands": json.loads(record["commands"]),
            "provenance": json.loads(record["provenance"]),
            "report": report,
        },
        "upstream_checks": {
            "github_check_runs": 0,
            "github_commit_statuses": 0,
            "combined_state": "pending",
            "note": (
                "no CI ran on the candidate: zero check-runs and zero statuses on "
                "the head SHA, so the combined state is pending for want of any "
                "report. This is the absence of CI, not a passing CI result."
            ),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sanitize(document), indent=2) + "\n")
    return out


def attempts(live: Path, out: Path) -> Path:
    connection = sqlite3.connect(live / "verifications.sqlite")
    connection.row_factory = sqlite3.Row
    rows = []
    for row in connection.execute("SELECT * FROM verifications ORDER BY id"):
        record = dict(row)
        rows.append(
            {
                "id": record["id"],
                "verdict": record["verdict"],
                "candidate_sha": record["candidate_sha"],
                "started_at": record["started_at"],
                "finished_at": record["finished_at"],
                "reason": record["reason"] or "",
                "failures": json.loads(record["failures"] or "[]"),
                "artifact": bool(record["artifact_path"]),
            }
        )
    document = {
        "total": len(rows),
        "blocked": sum(1 for row in rows if row["verdict"] == "blocked"),
        "passed": sum(1 for row in rows if row["verdict"] == PASSED),
        "attempts": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sanitize(document), indent=2) + "\n")
    return out


def main() -> None:
    live = Path(sys.argv[1])
    out = Path(sys.argv[2])
    export(live, out / "verification-passed.json", 8, 1)
    attempts(live, out / "attempts.json")
    # Read the published file back and grade *that*, so the evidence is what
    # was checked rather than what was in memory when it was written.
    published = json.loads((out / "verification-passed.json").read_text())
    report = published["verification"]["report"]
    verdict, reasons = grade(report, tuple(published["verification"]["cases"]))
    checks = sum(len(case["checks"]) for case in report["cases"])
    print(f"published file grades {verdict} {reasons} over {checks} checks")
    if verdict != PASSED or reasons:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
