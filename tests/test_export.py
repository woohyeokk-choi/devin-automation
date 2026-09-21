"""The evidence export must publish the values it claims to publish."""

from __future__ import annotations

from typing import Any

from portal.redaction import DROPPED, REDACTED, scrub
from scripts.export_verification import repo_of, sanitize


def _report() -> dict[str, Any]:
    return {
        "verification": {
            "report": {
                "cases": [
                    {
                        "case": "S2",
                        "verdict": "passed",
                        "checks": [
                            {
                                "name": "new_exploration_does_not_reuse_a_discarded_key",
                                "kind": "target",
                                "expected": False,
                                "observed": False,
                                "holds": True,
                                "note": "",
                            }
                        ],
                    }
                ]
            }
        }
    }


def test_the_published_checks_survive_the_depth_the_log_redactor_stops_at() -> None:
    check = sanitize(_report())["verification"]["report"]["cases"][0]["checks"][0]
    assert check["name"] == "new_exploration_does_not_reuse_a_discarded_key"
    assert check["kind"] == "target"
    assert check["holds"] is True
    assert check["observed"] is False


def test_the_log_redactor_would_have_dropped_them() -> None:
    # Why the export cannot simply call scrub(): the guard that protects the
    # event log from a hostile trace erases the evidence at this depth.
    dropped = scrub(_report())["verification"]["report"]["cases"][0]["checks"][0]
    assert set(dropped.values()) == {DROPPED}


def test_credential_shaped_text_is_still_scrubbed_at_any_depth() -> None:
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "Authorization: Bearer sekret"}}}}}}}
    leaf = sanitize(deep)["a"]["b"]["c"]["d"]["e"]["f"]["g"]
    assert "sekret" not in leaf


def test_a_value_named_by_its_key_is_redacted_however_deep_it_sits() -> None:
    # Unremarkable as text, so only the key says what it is.
    report = _report()
    report["verification"]["report"]["cases"][0]["checks"][0]["context"] = {
        "nested": {"password": "EXPORT_ONLY_TEST_CANARY"}
    }
    check = sanitize(report)["verification"]["report"]["cases"][0]["checks"][0]
    assert check["context"]["nested"]["password"] == REDACTED
    assert "EXPORT_ONLY_TEST_CANARY" not in str(check)
    # and the evidence around it is untouched
    assert check["name"] == "new_exploration_does_not_reuse_a_discarded_key"
    assert check["holds"] is True
    assert check["observed"] is False
    assert check["note"] == ""


def test_the_pull_request_url_names_the_repository_the_checks_are_read_from() -> None:
    assert repo_of("https://github.com/woohyeokk-choi/superset/pull/2") == (
        "woohyeokk-choi/superset"
    )
    assert repo_of("") == ""
