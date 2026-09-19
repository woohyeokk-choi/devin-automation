"""Readable Slack cards: structure, labelled links and truthful states.

No Slack and no credentials: the bot is the scripted client the other Slack
tests use, and every card here is built from a stored repair record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from portal.controller import AWAITING_MERGE, DISPATCHED, MERGED
from portal.notify import (
    SENT,
    Card,
    NotificationLog,
    Notifier,
    card_for,
)
from portal.slack_bot import SOURCE_LABEL

from test_slack_bot import FakeBot

HEAD = "7bb8de7b136f9afdc39d31b4c6809b3de467c461"
MERGE = "a" * 40

REPAIR: dict[str, Any] = {
    "id": 1,
    "incident_id": 1,
    "state": DISPATCHED,
    "attempt": 1,
    "follow_ups": 0,
    "acu_limit": 20,
    "deadline_utc": "2026-09-20T01:17:41.051266+00:00",
    "session_url": "https://app.devin.ai/sessions/e8b63e608d5a4397b114a296420695c7",
    "issue_url": "https://github.com/acme/superset/issues/5",
    "agent_pr_url": "https://github.com/acme/superset/pull/6",
    "pr_head_sha": HEAD,
    "attention": None,
    "terminal_reason": None,
    "updated_at": "2026-09-19T23:10:00+00:00",
}
INCIDENT: dict[str, Any] = {
    "id": 1,
    "scenario": "S1",
    "family": "omitted_row_limit_is_reset",
    "baseline_sha": "394bca55c792b7b3547e23f6e175a7cb0f0757e8",
    "events": [
        {
            "trace_id": "9f2c41ab7d5e4c108b",
            "assertion": {
                "name": "saved_row_limit_survives_a_sort_change",
                "holds": False,
                "expected": 137,
                "observed": 1000,
            },
        }
    ],
}


def _repair(**changes: Any) -> dict[str, Any]:
    return {**REPAIR, **changes}


def _types(card: Card) -> list[str]:
    return [block["type"] for block in card.blocks()]


def _text_of(card: Card) -> str:
    return json.dumps(card.blocks())


# --- structure -------------------------------------------------------------


def test_a_card_is_a_header_short_sections_and_one_context_footer() -> None:
    card = card_for("dispatched", REPAIR, INCIDENT)
    assert card is not None
    kinds = _types(card)
    assert kinds[0] == "header" and kinds[-1] == "context"
    # A headline, two short lines, one row of links, one footer: nothing else.
    assert kinds.count("section") == 3 and len(kinds) == 5
    assert card.blocks()[0]["text"]["type"] == "plain_text"


def test_the_footer_names_the_source_and_keeps_the_identifiers_short() -> None:
    card = card_for("dispatched", REPAIR, INCIDENT)
    assert card is not None
    footer = card.blocks()[-1]["elements"][0]["text"]
    assert footer.endswith(SOURCE_LABEL)
    assert "S1 ·" in footer and "Baseline 394bca55" in footer
    # The long forms belong in the audit record, not in the channel.
    assert INCIDENT["baseline_sha"] not in footer
    assert "Deadline Sep 20, 01:17 UTC" in footer


def test_links_are_labelled_and_no_raw_url_is_left_in_prose() -> None:
    card = card_for("candidate", _repair(state="candidate"), INCIDENT)
    assert card is not None
    links = card.blocks()[3]["text"]["text"]
    assert links == (
        f"<{REPAIR['agent_pr_url']}|Review PR #6> · "
        f"<{REPAIR['session_url']}|Open Devin> · "
        f"<{REPAIR['issue_url']}|View issue>"
    )
    for block in card.blocks()[:3]:
        assert "https://" not in json.dumps(block)


def test_a_missing_link_is_dropped_rather_than_labelled_with_nothing() -> None:
    card = card_for("candidate", _repair(agent_pr_url="", issue_url=""), INCIDENT)
    assert card is not None
    assert card.blocks()[3]["text"]["text"] == f"<{REPAIR['session_url']}|Open Devin>"


def test_fallback_text_is_one_short_meaningful_line() -> None:
    card = card_for("dispatched", REPAIR, INCIDENT)
    assert card is not None
    assert card.fallback.startswith("Investigating · Chart settings changed")
    assert len(card.fallback) <= 180 and "\n" not in card.fallback


def test_untrusted_text_cannot_forge_slack_markup() -> None:
    hostile = dict(INCIDENT, scenario="<https://evil.example|click> & co")
    card = card_for("dispatched", REPAIR, hostile)
    assert card is not None
    footer = card.blocks()[-1]["elements"][0]["text"]
    assert "&lt;https://evil.example|click&gt; &amp; co" in footer
    assert "<https://evil.example|click>" not in footer


# --- truthful lifecycle copy ----------------------------------------------


def test_the_first_card_says_what_happened_with_the_recorded_values() -> None:
    card = card_for("dispatched", REPAIR, INCIDENT)
    assert card is not None
    assert "reset the saved row limit from 137 to 1,000" in card.what
    assert "No merge without approval." in card.next_step
    # Nothing is confirmed yet, and no fix is claimed.
    assert "fixed" not in _text_of(card).lower()


def test_a_proposed_fix_is_not_described_as_accepted() -> None:
    card = card_for(
        "candidate",
        _repair(state="candidate", agent_output=json.dumps({"reproduced": True})),
        INCIDENT,
    )
    assert card is not None
    assert card.headline == "Fix proposed · Independent checks running"
    assert "Not yet accepted." in card.next_step
    body = _text_of(card).lower()
    assert "merged" not in body and "deployed" not in body


def test_a_preview_pass_is_never_a_merge_or_a_deployment() -> None:
    card = card_for(
        "awaiting_merge",
        _repair(
            state=AWAITING_MERGE,
            verification=json.dumps({"verdict": "passed", "checks": 13}),
        ),
        INCIDENT,
    )
    assert card is not None
    assert card.headline == "Preview passed · Awaiting your merge"
    assert "13 registered checks" in card.what
    footer = card.blocks()[-1]["elements"][0]["text"]
    assert footer.startswith("Not merged or deployed.")


def test_a_check_count_is_quoted_only_when_a_replay_recorded_one() -> None:
    card = card_for("awaiting_merge", _repair(state=AWAITING_MERGE), INCIDENT)
    assert card is not None
    assert "registered checks" not in card.what


def test_a_requested_recording_is_not_described_as_delivered() -> None:
    pending = card_for(
        "merge_verified",
        _repair(state=MERGED, merge_commit_sha=MERGE, media_state="requested"),
        INCIDENT,
    )
    delivered = card_for(
        "merge_verified",
        _repair(state=MERGED, merge_commit_sha=MERGE, media_state="delivered"),
        INCIDENT,
    )
    assert pending is not None and delivered is not None
    assert pending.headline.endswith("Recording pending")
    assert delivered.headline.endswith("Recording posted")
    assert "follows here" in pending.next_step
    assert "is in this thread" in delivered.next_step


def test_a_failure_card_gives_the_reason_and_what_a_person_must_do() -> None:
    card = card_for(
        "merge_failed",
        _repair(state=MERGED, merge_commit_sha=MERGE, attention="S1 replay failed"),
        INCIDENT,
    )
    assert card is not None
    assert "Needs you" in card.headline
    assert "S1 replay failed" in card.what
    assert "revert or to fix forward" in card.next_step


def test_a_blocked_attempt_reports_no_verdict() -> None:
    card = card_for(
        "blocked", _repair(attention="the candidate stack never started"), INCIDENT
    )
    assert card is not None
    assert card.headline == "Checks blocked · No verdict"
    assert "passed" not in _text_of(card).lower()


# --- delivery --------------------------------------------------------------


def _notifier(tmp_path: Path, bot: FakeBot) -> Notifier:
    return Notifier(
        log=NotificationLog(tmp_path / "notifications.sqlite"),
        webhook="",
        bot=bot,
        simulated=False,
    )


def test_a_published_card_is_sent_as_blocks_with_its_fallback_text(
    tmp_path: Path,
) -> None:
    bot = FakeBot()
    bot.simulated = False
    notifier = _notifier(tmp_path, bot)
    card = card_for("dispatched", REPAIR, INCIDENT)
    assert card is not None

    assert notifier.publish("1:dispatched:a", "dispatched", "long prose", 1, card=card) == SENT

    sent = bot.posts[0]
    assert sent["blocks"] == card.blocks()
    assert sent["text"] == card.fallback
    # The ledger keeps the detailed line the channel no longer carries.
    stored = notifier.log.get("1:dispatched:a")
    assert stored is not None and stored["text"] == "long prose"
    assert json.loads(str(stored["blocks"])) == card.blocks()


def test_the_same_event_is_not_published_twice(tmp_path: Path) -> None:
    bot = FakeBot()
    bot.simulated = False
    notifier = _notifier(tmp_path, bot)
    card = card_for("dispatched", REPAIR, INCIDENT)

    notifier.publish("1:dispatched:a", "dispatched", "prose", 1, card=card)
    notifier.publish("1:dispatched:a", "dispatched", "prose", 1, card=card)

    assert len(bot.posts) == 1


def test_editing_a_message_replaces_its_blocks_and_records_the_reason(
    tmp_path: Path,
) -> None:
    bot = FakeBot()
    bot.simulated = False
    notifier = _notifier(tmp_path, bot)
    card = card_for("dispatched", REPAIR, INCIDENT)
    assert notifier.publish("1:dispatched:a", "dispatched", "prose", 1, card=card) == SENT
    ts = "1789900000.000100"

    readable = card_for("awaiting_merge", _repair(state=AWAITING_MERGE), INCIDENT)
    assert readable is not None
    outcome = notifier.amend(ts, readable.fallback, "readability", card=readable)

    assert outcome["state"] == SENT
    assert bot.edits[0]["blocks"] == readable.blocks()
    assert bot.edits[0]["text"] == readable.fallback
    amendments = notifier.log.amendments()
    assert amendments[0]["reason"] == "readability"


def test_an_unknown_action_has_no_card_and_leaves_prose_alone() -> None:
    assert card_for("running", REPAIR, INCIDENT) is None


@pytest.mark.parametrize(
    "action",
    ["dispatched", "candidate", "awaiting_merge", "merge_verified", "stopped"],
)
def test_no_card_body_is_longer_than_a_readable_paragraph(action: str) -> None:
    card = card_for(action, _repair(terminal_reason="stopped by hand"), INCIDENT)
    assert card is not None
    for block in card.blocks():
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 600
