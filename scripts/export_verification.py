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
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.redaction import REDACTED, SENSITIVE_KEYS, scrub_text  # noqa: E402
from portal.verification import grade  # noqa: E402

PASSED = "passed"


def sanitize(value: Any) -> Any:
    """Redact credential-shaped text at every depth, preserving structure.

    A credential is often unremarkable as text — it is the key it sits under
    that names it — so a sensitive key is redacted wholesale, exactly as the
    event redactor does, before its value is ever walked.
    """
    if isinstance(value, dict):
        return {
            str(key): (
                REDACTED if str(key).lower() in SENSITIVE_KEYS else sanitize(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        return scrub_text(value)
    return scrub_text(str(value))


def repo_of(url: str) -> str:
    """The `owner/name` a pull request URL belongs to, or an empty string."""
    parts = url.split("/")
    return f"{parts[3]}/{parts[4]}" if len(parts) > 4 else ""


def upstream_checks(repo: str, sha: str) -> dict[str, Any]:
    """What GitHub reports for this head SHA, or an honest "not collected".

    Absent CI is not passing CI, and an unanswered query is not absent CI:
    a reader has to be able to tell which of the two they are looking at.
    """
    uncollected = {
        "collected": False,
        "note": f"GitHub check metadata for {sha} could not be read at export time",
    }
    if not repo or shutil.which("gh") is None:
        return uncollected
    try:
        runs = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        statuses = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{sha}/status"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if runs.returncode or statuses.returncode:
            return uncollected
        check_runs = json.loads(runs.stdout)
        status = json.loads(statuses.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return uncollected
    total = int(check_runs.get("total_count", 0))
    reported = len(status.get("statuses", []))
    return {
        "collected": True,
        "github_check_runs": total,
        "github_commit_statuses": reported,
        "combined_state": status.get("state", ""),
        "conclusions": sorted(
            {str(run.get("conclusion")) for run in check_runs.get("check_runs", [])}
        ),
        "note": (
            "read from GitHub at export time. A combined state of 'pending' with "
            "zero check-runs and zero statuses is the absence of CI, not a "
            "passing CI result."
        ),
    }


def export(live: Path, out: Path, verification_id: int, repair_id: int) -> Path:
    """Publish one verification, proving it belongs to the repair it claims."""
    verifications = sqlite3.connect(live / "verifications.sqlite")
    verifications.row_factory = sqlite3.Row
    repairs = sqlite3.connect(live / "repairs.sqlite")
    repairs.row_factory = sqlite3.Row
    found = verifications.execute(
        "SELECT * FROM verifications WHERE id = ?", (verification_id,)
    ).fetchone()
    owner = repairs.execute(
        "SELECT * FROM repairs WHERE id = ?", (repair_id,)
    ).fetchone()
    if found is None or owner is None:
        raise SystemExit(f"no such verification {verification_id} / repair {repair_id}")
    record = dict(found)
    repair = dict(owner)
    if record["repair_id"] != repair["id"]:
        raise SystemExit(
            f"verification {verification_id} belongs to repair "
            f"{record['repair_id']}, not {repair_id}"
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
        "upstream_checks": upstream_checks(
            repo_of(record["pr_url"] or repair["agent_pr_url"] or ""),
            record["candidate_sha"],
        ),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sanitize(document), indent=2) + "\n")
    return out


def attempts(live: Path, out: Path, repair_id: int) -> Path:
    """Every attempt made for one repair, blocked ones included."""
    connection = sqlite3.connect(live / "verifications.sqlite")
    connection.row_factory = sqlite3.Row
    rows = []
    for row in connection.execute(
        "SELECT * FROM verifications WHERE repair_id = ? ORDER BY id", (repair_id,)
    ):
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
        "repair_id": repair_id,
        "total": len(rows),
        "blocked": sum(1 for row in rows if row["verdict"] == "blocked"),
        "passed": sum(1 for row in rows if row["verdict"] == PASSED),
        "attempts": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sanitize(document), indent=2) + "\n")
    return out


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: export_verification.py <live-state> <out-dir> "
            "<repair-id> <verification-id>"
        )
    live = Path(sys.argv[1])
    out = Path(sys.argv[2])
    repair_id = int(sys.argv[3])
    verification_id = int(sys.argv[4])
    export(live, out / "verification-passed.json", verification_id, repair_id)
    attempts(live, out / "attempts.json", repair_id)
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
