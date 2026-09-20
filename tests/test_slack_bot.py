"""The Web API transport: threading, channel enforcement and clip uploads.

No Slack and no credentials anywhere. The bot is a scripted client that
records what it was asked to do and answers however the test tells it to, and
every clip is a temporary file written by the test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from portal.controller import VERIFIED
from portal.notify import (
    DISABLED,
    FAILED,
    HISTORICAL_THREADS,
    IN_THREAD,
    SENT,
    UNKNOWN,
    NotificationLog,
    Notifier,
    Recording,
    clip_upload_id,
    historical_message,
    historical_parents,
    result_message,
)
from portal.slack_bot import (
    APPROVED_CHANNEL,
    SOURCE_LABEL,
    SlackRejected,
    WebClientBot,
    channel_problem,
)
from portal.transport import Ambiguous, Refused

HEAD = "d" * 40
WEBHOOK = "https://hooks.slack.com/services/T0C30Q2H64W/B0000000000/xxxxxxxxxxxxxxxx"

REPAIR: dict[str, Any] = {
    "id": 2,
    "incident_id": 2,
    "state": VERIFIED,
    "attempt": 1,
    "follow_ups": 0,
    "session_id": "18b04127f4a44af6a9c71f9eb3eaba9e",
    "session_url": "https://app.devin.ai/sessions/18b04127f4a44af6a9c71f9eb3eaba9e",
    "issue_url": "https://github.com/acme/superset/issues/3",
    "agent_pr_url": "https://github.com/acme/superset/pull/4",
    "pr_head_sha": HEAD,
    "attention": None,
    "terminal_reason": None,
    "updated_at": "2026-09-19T22:10:00+00:00",
}
INCIDENT = {"id": 2, "scenario": "S1", "family": "omitted_row_limit_is_reset"}
PASSED_ATTEMPT = {
    "id": 9,
    "repair_id": 2,
    "candidate_sha": HEAD,
    "verdict": "passed",
    "checks": 13,
    "finished_at": "2026-09-19T22:12:00+00:00",
}
CAPTURE = Recording(
    url="https://example.com/s1-replay.mp4",
    case="S1",
    sha=HEAD,
    recorded_at="2026-09-19T23:00:00+00:00",
    scope="baseline failure then candidate pass",
)


class FakeBot:
    """A scripted Web API client. Simulated, and it says so."""

    simulated = True

    def __init__(self, *answers: Any) -> None:
        self.channel = APPROVED_CHANNEL
        self.answers = list(answers) or ["1789900000.000100"]
        self.posts: list[dict[str, Any]] = []
        self.authors: dict[str, dict[str, str]] = {}
        self.uploads: list[dict[str, str]] = []
        self.edits: list[dict[str, Any]] = []
        self.edit_answers: list[Any] = []

    def _answer(self, calls: int) -> Any:
        answer = self.answers[min(calls - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(
        self,
        text: str,
        *,
        thread_ts: str = "",
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        self.posts.append(
            {"text": text, "thread_ts": thread_ts, "blocks": blocks or []}
        )
        return str(self._answer(len(self.posts)))

    def amend(
        self, ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> str:
        self.edits.append({"ts": ts, "text": text, "blocks": blocks or []})
        answers = self.edit_answers or self.answers
        answer = answers[min(len(self.edits) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return str(answer)

    def author_of(self, ts: str) -> dict[str, str]:
        return self.authors.get(ts, {"readable": "", "detail": "not scripted"})

    def upload(
        self, path: Path, *, title: str, comment: str, thread_ts: str = ""
    ) -> str:
        self.uploads.append(
            {
                "path": str(path),
                "title": title,
                "comment": comment,
                "thread_ts": thread_ts,
            }
        )
        return str(self._answer(len(self.uploads)))


class LiveLookingBot(FakeBot):
    """A client that claims to reach Slack, for the simulation guards."""

    simulated = False


@pytest.fixture()
def log(tmp_path: Path) -> NotificationLog:
    return NotificationLog(tmp_path / "notifications.sqlite")


@pytest.fixture()
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "S1-exact-sha-replay.mp4"
    path.write_bytes(b"not really an mp4")
    return path


# --- one transport, never two ----------------------------------------------


def test_the_bot_is_preferred_and_the_webhook_never_also_posts(
    log: NotificationLog,
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, webhook=WEBHOOK, bot=bot)
    assert notifier.transport_name == "bot"
    assert notifier.publish("2:verified:d", "verified", "Verification passed", 2) == SENT
    assert len(bot.posts) == 1


def test_the_webhook_remains_the_fallback_when_no_bot_is_configured(
    log: NotificationLog,
) -> None:
    from tests.test_notify import Recorder

    wire = Recorder()
    notifier = Notifier(log=log, webhook=WEBHOOK, transport=wire)
    assert notifier.transport_name == "webhook"
    assert notifier.publish("2:verified:d", "verified", "Verification passed") == SENT
    assert len(wire.calls) == 1


# --- the approved channel --------------------------------------------------


def test_only_the_approved_channel_can_be_addressed() -> None:
    assert channel_problem(APPROVED_CHANNEL) == ""
    assert channel_problem("C09999999")
    with pytest.raises(ValueError):
        WebClientBot(token="xoxb-not-a-real-token", channel="C09999999")


def test_a_caller_supplied_channel_cannot_redirect_delivery(
    log: NotificationLog,
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot, channel="C09999999")
    assert not notifier.enabled and notifier.problem
    assert notifier.publish("2:verified:d", "verified", "text", 2) == DISABLED
    assert bot.posts == []


def test_a_bot_pointed_elsewhere_is_refused(log: NotificationLog) -> None:
    bot = FakeBot()
    bot.channel = "C09999999"
    notifier = Notifier(log=log, bot=bot)
    assert not notifier.enabled
    assert notifier.publish("2:verified:d", "verified", "text", 2) == DISABLED
    assert bot.posts == []


# --- simulation ------------------------------------------------------------


def test_a_simulated_notifier_refuses_a_real_bot(log: NotificationLog) -> None:
    bot = LiveLookingBot()
    notifier = Notifier(log=log, bot=bot, simulated=True)
    assert notifier.bot is None and not notifier.enabled
    assert notifier.publish("7:session:simulated-1", "dispatched", "text", 7) == DISABLED
    assert bot.posts == []


def test_a_simulated_record_is_refused_over_a_real_bot(log: NotificationLog) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    state = notifier.publish(
        "7:session:simulated-1", "dispatched", "text", 7, simulated_record=True
    )
    assert state == DISABLED
    assert bot.posts == []


# --- threading -------------------------------------------------------------


def _stored(known: Any, repair_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """A stored repair that matches one of the verified historical results."""
    return (
        REPAIR | {"id": repair_id, "incident_id": repair_id, "pr_head_sha": known.sha},
        {"id": repair_id, "scenario": known.case, "family": "f"},
    )


def test_updates_reply_under_the_repair_s_own_historical_result(
    log: NotificationLog,
) -> None:
    s2, s1 = HISTORICAL_THREADS
    parents, problems = historical_parents([_stored(s2, 1), _stored(s1, 2)])
    assert problems == []
    log.bootstrap_threads(APPROVED_CHANNEL, parents)

    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    notifier.publish("1:result:x", "result", "S2 update", 1)
    notifier.publish("2:result:x", "result", "S1 update", 2)

    assert bot.posts[0]["thread_ts"] == s2.parent_ts
    assert bot.posts[1]["thread_ts"] == s1.parent_ts
    # Two repairs, two conversations: neither is posted under the other.
    assert bot.posts[0]["thread_ts"] != bot.posts[1]["thread_ts"]


def test_a_fresh_ledger_inherits_no_historical_thread(log: NotificationLog) -> None:
    """Repair 1 of another deployment is not repair 1 of this one."""
    bot = FakeBot("1789900000.000700")
    notifier = Notifier(log=log, bot=bot)
    notifier.publish("1:dispatched:a", "dispatched", "investigation started", 1)

    assert log.threads() == [] or log.threads()[0]["source"] == "posted"
    assert bot.posts[0]["thread_ts"] == ""
    assert log.thread_of(1, APPROVED_CHANNEL) == "1789900000.000700"


def test_the_bootstrap_refuses_a_ledger_that_is_not_the_live_state(
    log: NotificationLog, tmp_path: Path
) -> None:
    """Those parents describe the repairs stored in runtime/live-state."""
    from portal.config import Settings
    from portal.notify import _bootstrap

    elsewhere = Settings(data_dir=tmp_path / "runtime" / "scratch")
    notifier = Notifier(log=log, bot=FakeBot())
    assert _bootstrap(notifier, elsewhere) == 2
    assert log.threads() == []


def test_a_historical_parent_needs_the_matching_case_and_accepted_head() -> None:
    s2, s1 = HISTORICAL_THREADS
    repair, incident = _stored(s2, 4)

    parents, _ = historical_parents([(repair, incident)])
    assert parents == {4: s2.parent_ts}

    # The same case at a different head, the other case at this head, and a
    # simulated row are all somebody else's repair.
    for wrong in (
        (repair | {"pr_head_sha": "c" * 40}, incident),
        (repair, incident | {"scenario": s1.case}),
        (repair | {"simulated": 1}, incident),
    ):
        parents, problems = historical_parents([wrong])
        assert parents == {} and problems


def test_a_repair_without_a_parent_adopts_its_first_message(
    log: NotificationLog,
) -> None:
    bot = FakeBot("1789900000.000100", "1789900000.000200")
    notifier = Notifier(log=log, bot=bot)
    notifier.publish("5:dispatched:a", "dispatched", "investigation started", 5)
    notifier.publish("5:verified:a", "verified", "verification passed", 5)

    assert bot.posts[0]["thread_ts"] == ""
    assert bot.posts[1]["thread_ts"] == "1789900000.000100"
    assert log.thread_of(5, APPROVED_CHANNEL) == "1789900000.000100"


def test_a_message_already_in_the_ledger_is_not_posted_again(
    log: NotificationLog,
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    notifier.publish("2:verified:d", "verified", "Verification passed", 2)
    # The same transition on the next worker pass, and again after a restart
    # that rebuilds the notifier from the same ledger.
    notifier.publish("2:verified:d", "verified", "Verification passed", 2)
    Notifier(log=log, bot=bot).publish(
        "2:verified:d", "verified", "Verification passed", 2
    )
    assert len(bot.posts) == 1


# --- the context label -----------------------------------------------------


def test_every_message_names_this_automation_as_its_source(
    log: NotificationLog,
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    _, kind, text = result_message(REPAIR, INCIDENT, PASSED_ATTEMPT)
    notifier.publish("2:result:d", kind, text, 2)
    assert bot.posts[0]["text"].endswith(f"_{SOURCE_LABEL}_")


def test_a_message_carries_no_credential_and_no_free_text_destination(
    log: NotificationLog,
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, webhook=WEBHOOK, bot=bot)
    _, kind, text = historical_message(REPAIR, INCIDENT, PASSED_ATTEMPT, CAPTURE)
    notifier.publish("2:backfill", kind, text, 2)
    posted = bot.posts[0]["text"]
    assert WEBHOOK not in posted and "xoxb-" not in posted
    assert "hooks.slack.com" not in posted


# --- uploads ---------------------------------------------------------------


def test_a_clip_is_uploaded_once_into_the_repair_s_thread(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot("F0123456789")
    notifier = Notifier(log=log, bot=bot)
    upload_id = clip_upload_id(2, CAPTURE, clip)

    first = notifier.attach(upload_id, clip, CAPTURE, REPAIR, PASSED_ATTEMPT)
    assert first == {"state": SENT, "detail": "", "file_id": "F0123456789"}
    assert bot.uploads[0]["thread_ts"] == ""
    assert log.upload(upload_id)["file_id"] == "F0123456789"

    # A repeated run, and a restart reading the same ledger, upload nothing.
    second = notifier.attach(upload_id, clip, CAPTURE, REPAIR, PASSED_ATTEMPT)
    third = Notifier(log=log, bot=bot).attach(
        upload_id, clip, CAPTURE, REPAIR, PASSED_ATTEMPT
    )
    assert second["state"] == SENT and third["state"] == SENT
    assert len(bot.uploads) == 1


def test_the_upload_id_is_the_same_across_runs_of_the_same_clip(clip: Path) -> None:
    assert clip_upload_id(2, CAPTURE, clip) == clip_upload_id(2, CAPTURE, clip)
    other = clip_upload_id(1, CAPTURE, clip)
    assert other != clip_upload_id(2, CAPTURE, clip)


def test_a_clip_from_another_commit_is_never_offered_to_slack(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    wrong = Recording(
        url=CAPTURE.url,
        case="S1",
        sha="a" * 40,
        recorded_at=CAPTURE.recorded_at,
        scope=CAPTURE.scope,
    )
    outcome = notifier.attach(
        clip_upload_id(2, wrong, clip), clip, wrong, REPAIR, PASSED_ATTEMPT
    )
    assert outcome["state"] == DISABLED and "not the head" in outcome["detail"]
    assert bot.uploads == [] and log.uploads() == []


def test_a_clip_of_an_unaccepted_candidate_is_never_uploaded(
    log: NotificationLog, clip: Path
) -> None:
    """Matching the head is not acceptance: the repair must be verified."""
    bot = FakeBot("F0123456789")
    notifier = Notifier(log=log, bot=bot)
    candidate = REPAIR | {"state": "candidate"}
    outcome = notifier.attach(
        clip_upload_id(2, CAPTURE, clip), clip, CAPTURE, candidate, PASSED_ATTEMPT
    )
    assert outcome["state"] == DISABLED and "candidate" in outcome["detail"]
    assert bot.uploads == [] and log.uploads() == []


def test_a_clip_without_a_passing_attempt_is_never_uploaded(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot("F0123456789")
    notifier = Notifier(log=log, bot=bot)
    blocked = PASSED_ATTEMPT | {"verdict": "blocked"}
    for attempt in (None, blocked):
        outcome = notifier.attach(
            clip_upload_id(2, CAPTURE, clip), clip, CAPTURE, REPAIR, attempt
        )
        assert outcome["state"] == DISABLED
    assert bot.uploads == [] and log.uploads() == []


def test_simulated_evidence_can_never_speak_for_an_accepted_head(
    log: NotificationLog, clip: Path
) -> None:
    """A simulated run measured nothing, whatever its rows say."""
    from portal.notify import result_problem

    assert "simulated" in result_problem(REPAIR | {"simulated": 1}, PASSED_ATTEMPT)
    assert "simulated" in result_problem(REPAIR, PASSED_ATTEMPT | {"simulated": 1})
    assert result_problem(REPAIR, PASSED_ATTEMPT) == ""

    bot = FakeBot("F0123456789")
    notifier = Notifier(log=log, bot=bot)
    outcome = notifier.attach(
        clip_upload_id(2, CAPTURE, clip),
        clip,
        CAPTURE,
        REPAIR,
        PASSED_ATTEMPT | {"simulated": 1},
    )
    assert outcome["state"] == DISABLED and "simulated" in outcome["detail"]
    assert bot.uploads == [] and log.uploads() == []


def test_a_clip_is_refused_when_the_verification_measured_another_commit(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot("F0123456789")
    notifier = Notifier(log=log, bot=bot)
    elsewhere = PASSED_ATTEMPT | {"candidate_sha": "b" * 40}
    outcome = notifier.attach(
        clip_upload_id(2, CAPTURE, clip), clip, CAPTURE, REPAIR, elsewhere
    )
    assert outcome["state"] == DISABLED and "measured" in outcome["detail"]
    assert bot.uploads == [] and log.uploads() == []


def test_a_clip_without_capture_metadata_stays_unpublished(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    bare = Recording(url=CAPTURE.url)
    outcome = notifier.attach(
        clip_upload_id(2, bare, clip), clip, bare, REPAIR, PASSED_ATTEMPT
    )
    assert outcome["state"] == DISABLED and outcome["file_id"] == ""
    assert bot.uploads == []


def test_an_upload_that_fails_is_not_recorded_as_delivered(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot(SlackRejected("not_in_channel"))
    notifier = Notifier(log=log, bot=bot)
    upload_id = clip_upload_id(2, CAPTURE, clip)
    outcome = notifier.attach(upload_id, clip, CAPTURE, REPAIR, PASSED_ATTEMPT)
    assert outcome["state"] == FAILED and outcome["file_id"] == ""
    assert log.upload(upload_id)["state"] == FAILED
    assert log.upload(upload_id)["file_id"] == ""


def test_an_upload_that_may_have_landed_is_unknown_rather_than_sent(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot(Ambiguous("ReadTimeout"))
    notifier = Notifier(log=log, bot=bot)
    upload_id = clip_upload_id(2, CAPTURE, clip)
    outcome = notifier.attach(upload_id, clip, CAPTURE, REPAIR, PASSED_ATTEMPT)
    assert outcome["state"] == UNKNOWN
    # Not retried from the ledger either: a second attempt could publish the
    # same clip twice.
    again = notifier.attach(upload_id, clip, CAPTURE, REPAIR, PASSED_ATTEMPT)
    assert again["state"] == UNKNOWN and len(bot.uploads) == 1


def test_a_simulated_repair_uploads_nothing(log: NotificationLog, clip: Path) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    outcome = notifier.attach(
        clip_upload_id(7, CAPTURE, clip),
        clip,
        CAPTURE,
        REPAIR | {"id": 7, "simulated": 1},
        PASSED_ATTEMPT | {"repair_id": 7},
        simulated_record=True,
    )
    assert outcome["state"] == DISABLED
    assert bot.uploads == [] and log.uploads() == []


def test_the_webhook_alone_cannot_attach_a_file(
    log: NotificationLog, clip: Path
) -> None:
    from tests.test_notify import Recorder

    notifier = Notifier(log=log, webhook=WEBHOOK, transport=Recorder())
    outcome = notifier.attach(
        clip_upload_id(2, CAPTURE, clip), clip, CAPTURE, REPAIR, PASSED_ATTEMPT
    )
    assert outcome["state"] == DISABLED and "SLACK_BOT_TOKEN" in outcome["detail"]


def test_a_missing_clip_file_is_refused_before_any_call(
    log: NotificationLog, tmp_path: Path
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    absent = tmp_path / "nothing.mp4"
    outcome = notifier.attach(
        "clip:2:dddddddddddd:000000000000", absent, CAPTURE, REPAIR, PASSED_ATTEMPT
    )
    assert outcome["state"] == DISABLED
    assert bot.uploads == []


# --- what a message may claim about a clip ---------------------------------


IN_THREAD_CAPTURE = Recording(
    url=IN_THREAD,
    case="S1",
    sha=HEAD,
    recorded_at="2026-09-19T23:00:00+00:00",
    scope="later CLI replay: baseline failure vs accepted-head pass",
)


def test_an_unsent_upload_is_never_described_as_uploaded(
    log: NotificationLog, clip: Path
) -> None:
    # A result is written before its file is offered, so the metadata alone
    # says a capture exists, not that Slack has it.
    _, _, before = result_message(
        REPAIR, INCIDENT, PASSED_ATTEMPT, recording=IN_THREAD_CAPTURE
    )
    assert "prepared for attachment in this thread" in before
    assert "uploaded" not in before

    upload_id = clip_upload_id(2, IN_THREAD_CAPTURE, clip)
    failed = Notifier(log=log, bot=FakeBot(SlackRejected("not_in_channel")))
    assert (
        failed.attach(upload_id, clip, IN_THREAD_CAPTURE, REPAIR, PASSED_ATTEMPT)[
            "state"
        ]
        == FAILED
    )
    assert log.delivered_clip(2, HEAD) == ""
    _, _, after_failure = result_message(
        REPAIR,
        INCIDENT,
        PASSED_ATTEMPT,
        recording=IN_THREAD_CAPTURE,
        attachment=log.delivered_clip(2, HEAD),
    )
    assert "uploaded" not in after_failure


def test_only_a_clip_slack_acknowledged_is_called_uploaded(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot("F0C33PMRVA6")
    notifier = Notifier(log=log, bot=bot)
    upload_id = clip_upload_id(2, IN_THREAD_CAPTURE, clip)
    outcome = notifier.attach(
        upload_id, clip, IN_THREAD_CAPTURE, REPAIR, PASSED_ATTEMPT
    )
    assert outcome["state"] == SENT and outcome["file_id"] == "F0C33PMRVA6"
    assert log.delivered_clip(2, HEAD) == "F0C33PMRVA6"
    # Another repair's ledger row cannot lend it that file.
    assert log.delivered_clip(3, HEAD) == ""
    _, _, text = result_message(
        REPAIR,
        INCIDENT,
        PASSED_ATTEMPT,
        recording=IN_THREAD_CAPTURE,
        attachment=log.delivered_clip(2, HEAD),
    )
    assert "uploaded to this thread as file `F0C33PMRVA6`" in text


def test_an_unknown_upload_is_not_promoted_to_a_delivered_clip(
    log: NotificationLog, clip: Path
) -> None:
    notifier = Notifier(log=log, bot=FakeBot(Ambiguous("ReadTimeout")))
    upload_id = clip_upload_id(2, IN_THREAD_CAPTURE, clip)
    assert (
        notifier.attach(upload_id, clip, IN_THREAD_CAPTURE, REPAIR, PASSED_ATTEMPT)[
            "state"
        ]
        == UNKNOWN
    )
    assert log.delivered_clip(2, HEAD) == ""


# --- correcting a message already posted -----------------------------------


def test_only_a_message_this_ledger_sent_can_be_edited(
    log: NotificationLog,
) -> None:
    bot = FakeBot("1789858187.458229")
    notifier = Notifier(log=log, bot=bot)
    assert notifier.publish("2:result:d", "result", "Result — original", 2) == SENT

    # A parent message, another ledger's correction, anything not recorded
    # here: refused without a call.
    assert notifier.amend("1789854458.454909", "rewritten", "test")["state"] == DISABLED
    assert bot.edits == []

    outcome = notifier.amend("1789858187.458229", "Result — corrected", "stale clause")
    assert outcome["state"] == SENT
    assert bot.edits[0]["ts"] == "1789858187.458229"
    assert SOURCE_LABEL in bot.edits[0]["text"]
    assert len(bot.posts) == 1

    # The superseded wording survives the edit Slack does not keep.
    [record] = log.amendments()
    assert "Result — original" in record["previous"]
    assert "Result — corrected" in record["replacement"]
    assert record["reason"] == "stale clause" and record["state"] == SENT


def test_an_edit_that_may_not_have_applied_is_unknown(log: NotificationLog) -> None:
    bot = FakeBot("1789858187.458229")
    bot.edit_answers = [Ambiguous("ReadTimeout")]
    notifier = Notifier(log=log, bot=bot)
    notifier.publish("2:result:d", "result", "Result — original", 2)
    outcome = notifier.amend("1789858187.458229", "Result — corrected", "why")
    assert outcome["state"] == UNKNOWN
    assert log.amendments()[0]["state"] == UNKNOWN
    # The ledger still holds what the channel may still be showing.
    assert "Result — original" in str(log.by_ts("1789858187.458229")["text"])


def test_the_webhook_alone_cannot_edit_a_message(log: NotificationLog) -> None:
    from tests.test_notify import Recorder

    notifier = Notifier(log=log, webhook=WEBHOOK, transport=Recorder())
    notifier.publish("2:result:d", "result", "Result — original", 2)
    log.record_ts(1, "1789858187.458229")
    outcome = notifier.amend("1789858187.458229", "corrected", "why")
    assert outcome["state"] == DISABLED and "SLACK_BOT_TOKEN" in outcome["detail"]


# --- the SDK client itself -------------------------------------------------


def test_the_sdk_client_does_not_retry_behind_the_ledger() -> None:
    bot = WebClientBot(token="xoxb-not-a-real-token")
    # The ledger owns retrying; the SDK's own handlers would multiply it.
    assert bot.client.retry_handlers == []
    assert bot.client.timeout == 15


def test_the_sdk_client_sends_blocks_without_unfurling_what_they_link_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = WebClientBot(token="xoxb-not-a-real-token")
    seen: dict[str, Any] = {}

    class Answer:
        def __init__(self, ts: str) -> None:
            self.data = {"ok": True, "ts": ts}

    def post(**kwargs: Any) -> Answer:
        seen.update(kwargs)
        return Answer("1789900000.000100")

    def update(**kwargs: Any) -> Answer:
        seen.update(kwargs)
        return Answer(str(kwargs["ts"]))

    monkeypatch.setattr(bot.client, "chat_postMessage", post)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]

    assert bot.post("fallback", blocks=blocks) == "1789900000.000100"
    assert seen["blocks"] == blocks and seen["text"] == "fallback"
    # A status card links to a pull request and a session: neither belongs in
    # the channel as an unrolled preview.
    assert seen["unfurl_links"] is False and seen["unfurl_media"] is False

    monkeypatch.setattr(bot.client, "chat_update", update)
    assert bot.amend("1789900000.000100", "fallback", blocks) == "1789900000.000100"
    assert seen["blocks"] == blocks and seen["link_names"] is False


def test_a_refusal_and_an_ambiguous_failure_are_told_apart(
    log: NotificationLog,
) -> None:
    refused = Notifier(log=log, bot=FakeBot(Refused("NameResolutionError")))
    assert refused.publish("2:a", "verified", "text", 2) == "pending"

    ambiguous = Notifier(log=log, bot=FakeBot(Ambiguous("ReadTimeout")))
    assert ambiguous.publish("2:b", "verified", "text", 2) == UNKNOWN


def test_symptom_footage_may_be_captioned_for_the_screen_it_shows(
    log: NotificationLog, clip: Path
) -> None:
    """A native Superset clip and a portal clip are both symptoms, not fixes."""
    baseline = "b" * 40
    capture = Recording(
        url=IN_THREAD,
        case="S1",
        sha=baseline,
        recorded_at="2026-09-20T00:55:00+00:00",
        scope="native Explore on the baseline, builder VM",
    )
    bot = FakeBot("F0CNATIVE")
    notifier = Notifier(log=log, bot=bot)

    outcome = notifier.attach(
        clip_upload_id(2, capture, clip),
        clip,
        capture,
        REPAIR,
        None,
        symptom_of=baseline,
        headline="Superset UI symptom replay — recorded after detection",
    )

    assert outcome["state"] == SENT and outcome["file_id"] == "F0CNATIVE"
    comment = bot.uploads[0]["comment"]
    assert comment.startswith("Superset UI symptom replay — recorded after detection")
    assert baseline[:12] in comment


def test_result_footage_cannot_be_recaptioned_into_something_else(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot("F0123456789")
    notifier = Notifier(log=log, bot=bot)

    notifier.attach(
        clip_upload_id(2, CAPTURE, clip),
        clip,
        CAPTURE,
        REPAIR,
        PASSED_ATTEMPT,
        headline="After merge — verified",
    )

    assert bot.uploads[0]["comment"].startswith("Replay:")
