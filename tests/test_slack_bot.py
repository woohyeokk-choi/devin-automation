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
    SENT,
    UNKNOWN,
    NotificationLog,
    Notifier,
    Recording,
    clip_upload_id,
    historical_message,
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
        self.posts: list[dict[str, str]] = []
        self.uploads: list[dict[str, str]] = []

    def _answer(self, calls: int) -> Any:
        answer = self.answers[min(calls - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, text: str, *, thread_ts: str = "") -> str:
        self.posts.append({"text": text, "thread_ts": thread_ts})
        return str(self._answer(len(self.posts)))

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


def test_updates_reply_under_the_repair_s_own_historical_result(
    log: NotificationLog,
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    notifier.publish("1:result:x", "result", "S2 update", 1)
    notifier.publish("2:result:x", "result", "S1 update", 2)

    assert bot.posts[0]["thread_ts"] == HISTORICAL_THREADS[1]
    assert bot.posts[1]["thread_ts"] == HISTORICAL_THREADS[2]
    # Two repairs, two conversations: neither is posted under the other.
    assert bot.posts[0]["thread_ts"] != bot.posts[1]["thread_ts"]


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

    first = notifier.attach(upload_id, clip, CAPTURE, REPAIR)
    assert first == {"state": SENT, "detail": "", "file_id": "F0123456789"}
    assert bot.uploads[0]["thread_ts"] == HISTORICAL_THREADS[2]
    assert log.upload(upload_id)["file_id"] == "F0123456789"

    # A repeated run, and a restart reading the same ledger, upload nothing.
    second = notifier.attach(upload_id, clip, CAPTURE, REPAIR)
    third = Notifier(log=log, bot=bot).attach(upload_id, clip, CAPTURE, REPAIR)
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
    outcome = notifier.attach(clip_upload_id(2, wrong, clip), clip, wrong, REPAIR)
    assert outcome["state"] == DISABLED and "not the head" in outcome["detail"]
    assert bot.uploads == [] and log.uploads() == []


def test_a_clip_without_capture_metadata_stays_unpublished(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    bare = Recording(url=CAPTURE.url)
    outcome = notifier.attach(clip_upload_id(2, bare, clip), clip, bare, REPAIR)
    assert outcome["state"] == DISABLED and outcome["file_id"] == ""
    assert bot.uploads == []


def test_an_upload_that_fails_is_not_recorded_as_delivered(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot(SlackRejected("not_in_channel"))
    notifier = Notifier(log=log, bot=bot)
    upload_id = clip_upload_id(2, CAPTURE, clip)
    outcome = notifier.attach(upload_id, clip, CAPTURE, REPAIR)
    assert outcome["state"] == FAILED and outcome["file_id"] == ""
    assert log.upload(upload_id)["state"] == FAILED
    assert log.upload(upload_id)["file_id"] == ""


def test_an_upload_that_may_have_landed_is_unknown_rather_than_sent(
    log: NotificationLog, clip: Path
) -> None:
    bot = FakeBot(Ambiguous("ReadTimeout"))
    notifier = Notifier(log=log, bot=bot)
    upload_id = clip_upload_id(2, CAPTURE, clip)
    outcome = notifier.attach(upload_id, clip, CAPTURE, REPAIR)
    assert outcome["state"] == UNKNOWN
    # Not retried from the ledger either: a second attempt could publish the
    # same clip twice.
    again = notifier.attach(upload_id, clip, CAPTURE, REPAIR)
    assert again["state"] == UNKNOWN and len(bot.uploads) == 1


def test_a_simulated_repair_uploads_nothing(log: NotificationLog, clip: Path) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    outcome = notifier.attach(
        clip_upload_id(7, CAPTURE, clip),
        clip,
        CAPTURE,
        REPAIR | {"id": 7, "simulated": 1},
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
        clip_upload_id(2, CAPTURE, clip), clip, CAPTURE, REPAIR
    )
    assert outcome["state"] == DISABLED and "SLACK_BOT_TOKEN" in outcome["detail"]


def test_a_missing_clip_file_is_refused_before_any_call(
    log: NotificationLog, tmp_path: Path
) -> None:
    bot = FakeBot()
    notifier = Notifier(log=log, bot=bot)
    absent = tmp_path / "nothing.mp4"
    outcome = notifier.attach("clip:2:dddddddddddd:000000000000", absent, CAPTURE, REPAIR)
    assert outcome["state"] == DISABLED
    assert bot.uploads == []


# --- the SDK client itself -------------------------------------------------


def test_the_sdk_client_does_not_retry_behind_the_ledger() -> None:
    bot = WebClientBot(token="xoxb-not-a-real-token")
    # The ledger owns retrying; the SDK's own handlers would multiply it.
    assert bot.client.retry_handlers == []
    assert bot.client.timeout == 15


def test_a_refusal_and_an_ambiguous_failure_are_told_apart(
    log: NotificationLog,
) -> None:
    refused = Notifier(log=log, bot=FakeBot(Refused("NameResolutionError")))
    assert refused.publish("2:a", "verified", "text", 2) == "pending"

    ambiguous = Notifier(log=log, bot=FakeBot(Ambiguous("ReadTimeout")))
    assert ambiguous.publish("2:b", "verified", "text", 2) == UNKNOWN
