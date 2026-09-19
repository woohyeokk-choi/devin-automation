"""Outbound status notifications, with no Slack anywhere.

Every delivery here goes to a recording transport or to a throwaway HTTP
server on loopback. What is real is the notifier, its ledger and the message
text; only the far end is local.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from portal.controller import (
    CANDIDATE,
    DISPATCHED,
    NEEDS_ATTENTION,
    TERMINAL,
    VERIFIED,
)
from portal.notify import (
    DISABLED,
    FAILED,
    MAX_ATTEMPTS,
    PENDING,
    SENT,
    UNKNOWN,
    NotificationLog,
    Notifier,
    Recording,
    correction_message,
    historical_message,
    PREVIEW_ONLY,
    _fingerprint,
    message_for,
    reconcile,
    recording_problem,
    result_message,
    result_problem,
    webhook_problem,
)
from portal.redaction import REDACTED, scrub
from portal.transport import Ambiguous, HttpTransport, Refused, Response

WEBHOOK = "https://hooks.slack.com/services/T0C30Q2H64W/B0000000000/xxxxxxxxxxxxxxxx"


class Recorder:
    """A wire that records what was posted and answers however it is told."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers) or [Response(200)]
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        self.calls.append({"method": method, "url": url, "json": json})
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture()
def log(tmp_path: Path) -> NotificationLog:
    return NotificationLog(tmp_path / "notifications.sqlite")


def clock_from(start: datetime) -> Any:
    return lambda: start


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
    "pr_head_sha": "f" * 40,
    "attention": None,
    "terminal_reason": None,
    "updated_at": "2026-09-19T22:10:00+00:00",
}
INCIDENT = {"id": 2, "scenario": "S1", "family": "omitted_row_limit_is_reset"}


# --- the webhook itself ----------------------------------------------------


def test_only_a_slack_webhook_url_is_accepted() -> None:
    assert webhook_problem(WEBHOOK) == ""
    # A plain-text post, another host, or an arbitrary path on the right host
    # are all configuration mistakes rather than things to try once.
    assert webhook_problem(WEBHOOK.replace("https", "http"))
    assert webhook_problem("https://hooks.slack.example.com/services/a/b/c")
    assert webhook_problem("https://hooks.slack.com/anything/else")


def test_a_missing_webhook_records_the_message_and_sends_nothing(
    log: NotificationLog,
) -> None:
    wire = Recorder()
    notifier = Notifier(log=log, webhook="", transport=wire)
    assert notifier.publish("e1", "verified", "text") == DISABLED
    assert wire.calls == []
    # The lifecycle is still readable: the message exists, unsent.
    assert log.get("e1")["state"] == DISABLED


def test_a_malformed_webhook_is_refused_rather_than_tried(log: NotificationLog) -> None:
    wire = Recorder()
    notifier = Notifier(log=log, webhook="https://example.com/hook", transport=wire)
    assert not notifier.enabled and notifier.problem
    assert notifier.publish("e1", "verified", "text") == DISABLED
    assert wire.calls == []


# --- delivery --------------------------------------------------------------


def test_a_message_is_posted_once_and_never_repeated(log: NotificationLog) -> None:
    wire = Recorder(Response(200))
    notifier = Notifier(log=log, webhook=WEBHOOK, transport=wire)
    assert notifier.publish("2:verified:abc", "verified", "Verification passed") == SENT
    # The same transition seen again on the next worker pass: one message.
    assert notifier.publish("2:verified:abc", "verified", "Verification passed") == SENT
    assert len(wire.calls) == 1
    body = wire.calls[0]["json"]
    assert body["text"] == "Verification passed"
    assert body["unfurl_links"] is False and body["unfurl_media"] is False


def test_an_http_error_is_retried_with_backoff_and_then_given_up(
    log: NotificationLog,
) -> None:
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    wire = Recorder(Response(500, {"message": "no"}))
    notifier = Notifier(log=log, webhook=WEBHOOK, transport=wire, now=lambda: now)
    assert notifier.publish("e1", "verified", "text") == PENDING
    # Nothing is due yet, so a tick does not hammer a failing endpoint.
    assert notifier.deliver_due() == []
    assert len(wire.calls) == 1

    states = []
    for _ in range(MAX_ATTEMPTS):
        now = now + timedelta(hours=1)
        notifier.now = lambda: now
        notifier._clock = lambda: now
        states.extend(notifier.deliver_due())
    assert states[-1] == FAILED
    assert log.get("e1")["state"] == FAILED
    assert len(wire.calls) == MAX_ATTEMPTS
    # Given up, so a later pass stays quiet rather than retrying forever.
    assert notifier.deliver_due() == []


def test_an_ambiguous_delivery_is_recorded_and_not_retried(log: NotificationLog) -> None:
    wire = Recorder(Ambiguous("ReadTimeout"))
    notifier = Notifier(log=log, webhook=WEBHOOK, transport=wire)
    # The body may have arrived; sending again would post the same line twice.
    assert notifier.publish("e1", "verified", "text") == UNKNOWN
    assert notifier.deliver_due() == []
    assert len(wire.calls) == 1
    assert log.get("e1")["state"] == UNKNOWN


def test_a_refusal_is_retried(log: NotificationLog) -> None:
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    wire = Recorder(Refused("NewConnectionError"), Response(200))
    notifier = Notifier(log=log, webhook=WEBHOOK, transport=wire, now=lambda: now)
    assert notifier.publish("e1", "verified", "text") == PENDING
    now = now + timedelta(minutes=5)
    notifier._clock = lambda: now
    assert notifier.deliver_due() == [SENT]


# --- the secret ------------------------------------------------------------


def test_the_webhook_never_reaches_the_ledger_or_a_log(log: NotificationLog) -> None:
    class Exploding:
        def request(self, *args: Any, **kwargs: Any) -> Response:
            # The shape a `requests` error takes: the URL inside the message.
            raise Refused(f"failed to post to {WEBHOOK}")

    notifier = Notifier(log=log, webhook=WEBHOOK, transport=Exploding())
    notifier.publish("e1", "verified", "text")
    detail = str(log.get("e1")["detail"])
    assert WEBHOOK not in detail and "xxxxxxxxxxxxxxxx" not in detail


def test_a_webhook_url_is_scrubbed_at_any_depth() -> None:
    # A webhook URL is a bearer credential in path form, and carries no
    # `key=value` shape for the generic rules to catch.
    canary = {"deep": {"config": [f"posting to {WEBHOOK} now"]}}
    assert WEBHOOK not in json.dumps(scrub(canary))
    assert REDACTED in json.dumps(scrub(canary))


def test_a_bot_token_is_scrubbed_at_any_depth() -> None:
    # The Web API credential is bare in SDK errors and tracebacks, and is
    # worth as much as the webhook to anyone who reads it.
    token = "xoxb-000000000000-canary-not-a-real-token"
    canary = {"deep": {"error": [f"not_authed while using {token}"]}}
    assert token not in json.dumps(scrub(canary))
    assert REDACTED in json.dumps(scrub(canary))


# --- what is worth saying --------------------------------------------------


@pytest.mark.parametrize(
    "action", ["running", "deferred", "skipped", "queued", "in_flight", "proposed"]
)
def test_progress_noise_is_not_announced(action: str) -> None:
    assert message_for(action, REPAIR, INCIDENT) is None


def test_the_verified_message_carries_the_head_and_the_preview_caveat() -> None:
    event_id, kind, text = message_for("verified", REPAIR, INCIDENT)
    assert event_id == f"2:verified:{'f' * 40}"
    assert kind == "verified"
    assert "f" * 40 in text
    assert "verified in isolated preview; not merged/deployed" in text
    assert "https://github.com/acme/superset/pull/4" in text
    assert "S1" in text


def test_each_lifecycle_moment_has_a_stable_distinct_id() -> None:
    dispatched = message_for("dispatched", REPAIR | {"state": DISPATCHED}, INCIDENT)
    candidate = message_for("candidate", REPAIR | {"state": CANDIDATE}, INCIDENT)
    attention = message_for(
        "parked", REPAIR | {"state": NEEDS_ATTENTION, "attention": "PR unusable"}, INCIDENT
    )
    ids = {message[0] for message in (dispatched, candidate, attention)}
    assert len(ids) == 3
    assert "18b04127f4a44af6a9c71f9eb3eaba9e" in dispatched[0]
    assert "provisional" in candidate[2] and "decides" in candidate[2]
    assert "PR unusable" in attention[2]


def test_slack_markup_in_a_stored_reason_cannot_forge_a_message() -> None:
    hostile = REPAIR | {
        "state": NEEDS_ATTENTION,
        "attention": "<!channel> see <https://evil.example|here> & hurry",
    }
    _, _, text = message_for("parked", hostile, INCIDENT)
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text
    assert "<https://evil.example|here>" not in text


def test_an_expected_denial_never_reaches_the_notifier(tmp_path: Path) -> None:
    """A 403 a role is supposed to get is not an incident, so it is not news."""
    from portal.incidents import IncidentStore

    from test_incidents import REPO, event

    store = IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)
    denial = event(
        event_id="n1",
        outcome="expected_denial",
        assertion="restricted_user_cannot_write",
        scenario="N1",
    )
    store.observe(denial)
    assert store.list() == []


# --- history ---------------------------------------------------------------


def test_a_backfill_is_marked_historical_and_runs_only_once(
    log: NotificationLog,
) -> None:
    wire = Recorder(Response(200))
    notifier = Notifier(log=log, webhook=WEBHOOK, transport=wire)
    attempt = {
        "verdict": "passed",
        "finished_at": "2026-09-19T22:07:55+00:00",
        "cases": "S1,N1",
    }
    event_id, kind, text = historical_message(REPAIR, INCIDENT, attempt)
    assert text.startswith("Historical result — repair ran earlier.")
    assert "2026-09-19T22:07:55+00:00" in text and "f" * 40 in text
    assert "verified in isolated preview; not merged/deployed" in text

    assert notifier.publish(event_id, kind, text, 2) == SENT
    # Running the command again must not spam the channel.
    assert notifier.publish(*historical_message(REPAIR, INCIDENT, attempt), 2) == SENT
    assert len(wire.calls) == 1


# --- against a real socket -------------------------------------------------


class _Hook(BaseHTTPRequestHandler):
    received: list[bytes] = []
    status = 200
    location = ""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        length = int(self.headers.get("Content-Length", "0"))
        _Hook.received.append(self.rfile.read(length))
        self.send_response(_Hook.status)
        if _Hook.location:
            self.send_header("Location", _Hook.location)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture()
def hook() -> Any:
    _Hook.received, _Hook.status, _Hook.location = [], 200, ""
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_delivery_over_a_real_socket_reaches_the_server(
    log: NotificationLog, hook: HTTPServer
) -> None:
    url = f"http://127.0.0.1:{hook.server_port}/services/T/B/x"
    notifier = Notifier(log=log, webhook=WEBHOOK, transport=HttpTransport(timeout=5))
    # The host check is about configuration; this exercises the real wire.
    notifier.webhook = url
    assert notifier.publish("e1", "verified", "hello") == SENT
    assert json.loads(_Hook.received[0])["text"] == "hello"


def test_a_redirect_is_not_followed(log: NotificationLog, hook: HTTPServer) -> None:
    """A 302 must not hand the message body to whatever host it names."""
    _Hook.status, _Hook.location = 302, "http://127.0.0.1:1/elsewhere"
    url = f"http://127.0.0.1:{hook.server_port}/services/T/B/x"
    notifier = Notifier(
        log=log, webhook=WEBHOOK, transport=HttpTransport(timeout=5, allow_redirects=False)
    )
    notifier.webhook = url
    assert notifier.publish("e1", "verified", "hello") == PENDING
    assert len(_Hook.received) == 1


# --- what the lifecycle is allowed to claim --------------------------------

#: An incident as the store hands it over: the failing action, the contract
#: that broke, and why it was admitted.
ASSESSED = INCIDENT | {
    "baseline_sha": "394bca55c792b7b3547e23f6e175a7cb0f0757e8",
    "occurrence_count": 3,
    "admission_reason": "registered S1 contract failure on the baseline revision",
    "events": [
        {
            "trace_id": "trace-77",
            "operation": "portal.update_chart_sort",
            "assertion": {
                "name": "omitted_row_limit_preserved",
                "expected": 137,
                "observed": 1000,
                "holds": False,
            },
        }
    ],
}


def test_the_first_message_says_suspected_and_carries_the_assessment() -> None:
    """An eligibility rule ran. Nothing has reproduced anything yet."""
    _, kind, text = message_for(
        "dispatched", REPAIR | {"state": DISPATCHED, "acu_limit": 20}, ASSESSED
    )
    assert kind == "investigation_started"
    assert text.startswith("Suspected defect — investigation started.")
    assert "confirmed" not in text
    # what failed, expected versus observed, and where it was seen
    assert "portal.update_chart_sort" in text
    assert "137" in text and "1000" in text
    assert "trace-77" in text and "394bca55c792" in text
    # why it is eligible, and what the bounded plan is
    assert "registered S1 contract failure" in text
    assert "reproduces first" in text and "20 requested ACUs" in text
    assert "replay of the pull request SHA decides acceptance" in text


def test_a_pull_request_is_provisional_and_quotes_the_session_as_the_source() -> None:
    _, _, text = message_for(
        "candidate",
        REPAIR
        | {
            "state": CANDIDATE,
            "agent_output": json.dumps(
                {
                    "reproduced": True,
                    "classification": "product_defect",
                    "summary": "form_data key reused after discard",
                }
            ),
        },
        ASSESSED,
    )
    assert "provisional" in text
    assert "session reports the failure confirmed (product_defect)" in text
    assert "form_data key reused after discard" in text
    assert "Nothing is accepted yet" in text
    assert "verified in isolated preview" not in text


def test_a_failed_or_blocked_verification_is_not_completion() -> None:
    _, _, failed = message_for(
        "followed_up", REPAIR | {"follow_ups": 1}, ASSESSED
    )
    _, _, blocked = message_for(
        "blocked", REPAIR | {"attention": "candidate stack init exited 1"}, ASSESSED
    )
    for text in (failed, blocked):
        assert "not completed" in text
        assert "Next:" in text
        assert "verified in isolated preview" not in text


def test_the_final_message_points_at_the_replay_and_invents_no_video() -> None:
    _, _, text = message_for(
        "verified",
        REPAIR
        | {
            "verification": json.dumps(
                {"verdict": "passed", "attempt_id": 9, "at": "2026-09-19T22:24:51+00:00"}
            )
        },
        ASSESSED,
    )
    assert "registered checks replayed in attempt 9" in text
    assert "verified in isolated preview; not merged/deployed" in text
    assert "recording" not in text

    _, _, historical = historical_message(
        REPAIR, ASSESSED, {"verdict": "passed", "cases": "S1,N1"},
        Recording(
            url="https://example.invalid/replay.mp4",
            case="S1",
            sha="f" * 40,
            recorded_at="2026-09-19T22:16:48+00:00",
        ),
    )
    assert historical.startswith("Historical result — repair ran earlier.")
    assert "recording https://example.invalid/replay.mp4" in historical


def test_a_recording_speaks_for_a_head_only_if_it_says_it_ran_there() -> None:
    """The capture states its own revision; the repair never lends it one."""
    good = Recording(
        url="https://example.invalid/replay-s1.mp4",
        case="S1",
        sha="F" * 40,
        recorded_at="2026-09-19T22:16:48+00:00",
        scope="baseline failure then the accepted head passing",
    )
    _, _, plain = historical_message(REPAIR, ASSESSED, {"verdict": "passed"}, good)
    assert recording_problem(good, REPAIR) == ""
    assert "S1 captured 2026-09-19T22:16:48+00:00 against `ffffffffffff`" in plain
    assert "shows baseline failure then the accepted head passing" in plain

    # A capture of another revision is footage of another product.
    elsewhere = Recording(
        url=good.url, case="S1", sha="a" * 40, recorded_at=good.recorded_at
    )
    assert recording_problem(elsewhere, REPAIR) == (
        "the capture ran against aaaaaaaaaaaa, not the head ffffffffffff"
    )
    _, _, wrong = historical_message(REPAIR, ASSESSED, {"verdict": "passed"}, elsewhere)
    assert "recording pending — the capture ran against aaaaaaaaaaaa" in wrong
    assert good.url not in wrong

    # A bare link claims nothing, so it is not published as footage.
    bare = Recording(url=good.url)
    assert "no case" in recording_problem(bare, REPAIR)
    _, _, unstated = historical_message(REPAIR, ASSESSED, {"verdict": "passed"}, bare)
    assert "recording pending" in unstated and good.url not in unstated

    # An abbreviated revision cannot be compared to a head.
    short = Recording(
        url=good.url, case="S1", sha="ffffff", recorded_at=good.recorded_at
    )
    assert recording_problem(short, REPAIR) == (
        "the capture's revision is not a full commit sha"
    )

    signed = Recording(
        url="https://cdn.example/clip.mp4?X-Amz-Signature=deadbeef",
        case="S1",
        sha="f" * 40,
        recorded_at=good.recorded_at,
    )
    _, _, withheld = historical_message(REPAIR, ASSESSED, {"verdict": "passed"}, signed)
    assert "deadbeef" not in withheld
    assert "not a shareable https link" in withheld

    _, _, none = historical_message(REPAIR, ASSESSED, {"verdict": "passed"})
    assert "recording" not in none

    # A clip published as a file in the thread has no link to give, and says
    # so rather than borrowing one; its metadata is checked as strictly.
    from portal.notify import IN_THREAD

    in_thread = Recording(
        url=IN_THREAD,
        case="S1",
        sha="f" * 40,
        recorded_at=good.recorded_at,
        scope="later CLI replay",
    )
    assert recording_problem(in_thread, REPAIR) == ""
    _, _, attached = historical_message(
        REPAIR, ASSESSED, {"verdict": "passed"}, in_thread
    )
    assert "recording prepared for attachment in this thread — S1 captured" in attached
    assert recording_problem(
        Recording(url=IN_THREAD, case="S1", sha="a" * 40, recorded_at=good.recorded_at),
        REPAIR,
    ).startswith("the capture ran against")


# --- restart recovery ------------------------------------------------------


def _ledger(tmp_path: Path, name: str = "n.sqlite") -> NotificationLog:
    return NotificationLog(tmp_path / name)


def test_a_restart_announces_an_outcome_whose_message_died_with_the_process(
    tmp_path: Path,
) -> None:
    """The state is committed; the Decision that carried it is gone."""
    wire = Recorder(Response(200))
    notifier = Notifier(log=_ledger(tmp_path), webhook=WEBHOOK, transport=wire)
    # The ledger takes responsibility before the repair reaches its outcome,
    # which is what separates a lost message from history.
    notifier.log.watermark("2026-09-19T22:00:00.000+00:00")

    announced = reconcile([REPAIR], notifier, lambda _id: INCIDENT)

    assert announced == [f"2:verified:{'f' * 40}"]
    # The channel gets the card; the ledger keeps the detailed line.
    assert "Preview passed" in wire.calls[0]["json"]["text"]
    stored = notifier.log.get(f"2:verified:{'f' * 40}")
    assert stored is not None and PREVIEW_ONLY in str(stored["text"])
    # A second restart owes the channel nothing.
    assert reconcile([REPAIR], notifier, lambda _id: INCIDENT) == []
    assert len(wire.calls) == 1


def test_a_restart_does_not_replay_results_older_than_the_ledger(
    tmp_path: Path,
) -> None:
    """Backfill exists for history; a restart is not a re-announcement."""
    wire = Recorder(Response(200))
    notifier = Notifier(log=_ledger(tmp_path), webhook=WEBHOOK, transport=wire)
    notifier.log.watermark("2026-09-19T23:00:00.000+00:00")

    assert reconcile([REPAIR], notifier, lambda _id: INCIDENT) == []
    assert wire.calls == []


def test_the_watermark_is_written_once_and_never_moves(tmp_path: Path) -> None:
    log = _ledger(tmp_path)
    first = log.watermark("2026-09-19T22:00:00.000+00:00")
    assert log.watermark("2026-09-20T09:00:00.000+00:00") == first


def test_reconciliation_covers_the_lifecycle_states_and_ignores_the_rest(
    tmp_path: Path,
) -> None:
    dispatched = REPAIR | {"id": 3, "state": DISPATCHED, "acu_limit": 20}
    running = REPAIR | {"id": 4, "state": "running"}
    parked = REPAIR | {"id": 5, "state": NEEDS_ATTENTION, "attention": "no verifier"}
    notifier = Notifier(log=_ledger(tmp_path), webhook="")
    notifier.log.watermark("2026-09-19T22:00:00.000+00:00")

    announced = reconcile(
        [dispatched, running, parked], notifier, lambda _id: INCIDENT
    )

    assert announced == [
        "3:session:18b04127f4a44af6a9c71f9eb3eaba9e",
        f"5:attention:{_fingerprint('no verifier')}",
    ]
    assert [row["kind"] for row in notifier.log.list()] == [
        "investigation_started",
        "needs_attention",
    ]


def test_reconciliation_never_writes_to_a_repair(tmp_path: Path) -> None:
    """A message may not change the thing it reports on."""
    before = dict(REPAIR)
    notifier = Notifier(log=_ledger(tmp_path), webhook="")
    notifier.log.watermark("2026-09-19T22:00:00.000+00:00")
    reconcile([REPAIR], notifier, lambda _id: INCIDENT)
    assert REPAIR == before


def test_one_unreportable_repair_does_not_silence_the_others(
    tmp_path: Path,
) -> None:
    broken = {"id": 9, "state": VERIFIED}  # no incident_id
    notifier = Notifier(log=_ledger(tmp_path), webhook="")
    notifier.log.watermark("2026-09-19T22:00:00.000+00:00")

    assert reconcile([broken, REPAIR], notifier, lambda _id: INCIDENT) == [
        f"2:verified:{'f' * 40}"
    ]


# --- simulated rows --------------------------------------------------------

SIMULATED_ROW = REPAIR | {"id": 7, "simulated": 1, "state": DISPATCHED}


def test_a_simulated_record_is_refused_by_a_live_notifier(
    tmp_path: Path,
) -> None:
    """The row's own flag stops it, not the wiring that read it.

    A real coordinator can be pointed at a database holding scripted
    repairs; its providers look live, so only the record says otherwise.
    """
    wire = Recorder()
    notifier = Notifier(log=_ledger(tmp_path), webhook=WEBHOOK, transport=wire)

    state = notifier.publish(
        "7:session:simulated-1", "investigation_started", "text",
        7, simulated_record=True,
    )

    assert state == DISABLED and wire.calls == []
    row = notifier.log.get("7:session:simulated-1")
    assert row["detail"] == "simulated repair record: real delivery refused"
    assert row["text"].startswith("[SIMULATED] ")


def test_reconciliation_refuses_simulated_rows_over_a_real_webhook(
    tmp_path: Path,
) -> None:
    wire = Recorder()
    notifier = Notifier(log=_ledger(tmp_path), webhook=WEBHOOK, transport=wire)
    notifier.log.watermark("2026-09-19T22:00:00.000+00:00")

    reconcile([SIMULATED_ROW, REPAIR], notifier, lambda _id: INCIDENT)

    states = {row["repair_id"]: row["state"] for row in notifier.log.list()}
    assert states == {7: DISABLED, 2: SENT}
    assert len(wire.calls) == 1
    assert "[SIMULATED]" not in wire.calls[0]["json"]["text"]


def test_a_backfill_of_a_simulated_row_posts_nothing(tmp_path: Path) -> None:
    """The CLI's own delivery call refuses the row it just read."""
    wire = Recorder()
    notifier = Notifier(log=_ledger(tmp_path), webhook=WEBHOOK, transport=wire)
    event_id, kind, text = historical_message(SIMULATED_ROW, INCIDENT, None)

    state = notifier.publish(
        event_id, kind, text, 7,
        simulated_record=bool(SIMULATED_ROW.get("simulated")),
    )

    assert state == DISABLED and wire.calls == []


# --- the result follow-up --------------------------------------------------

PASSED_ATTEMPT = {
    "id": 11,
    "verdict": "passed",
    "candidate_sha": "f" * 40,
    "cases": "S1,N1",
    "finished_at": "2026-09-19T22:24:51.208+00:00",
}
RECORDING = Recording(
    url="https://example.invalid/replay-s1.mp4",
    case="S1",
    sha="f" * 40,
    recorded_at="2026-09-19T22:16:48+00:00",
    scope="S1 replay at the accepted head",
)


def test_a_result_is_separately_identified_and_carries_its_recording(
    tmp_path: Path,
) -> None:
    """Backfill has already been sent; a later video still has to arrive."""
    wire = Recorder()
    notifier = Notifier(log=_ledger(tmp_path), webhook=WEBHOOK, transport=wire)
    backfill_id, kind, text = historical_message(REPAIR, INCIDENT, PASSED_ATTEMPT)
    notifier.publish(backfill_id, kind, text, 2)

    event_id, kind, text = result_message(
        REPAIR, INCIDENT, PASSED_ATTEMPT, recording=RECORDING
    )
    assert notifier.publish(event_id, kind, text, 2) == SENT

    assert event_id == f"2:result:{'f' * 40}:{_fingerprint(RECORDING.url)}"
    assert event_id != backfill_id
    assert RECORDING.url in wire.calls[-1]["json"]["text"]
    assert PREVIEW_ONLY in text and "S1,N1" in text
    # The same link again is the same event.
    assert notifier.publish(event_id, kind, text, 2) == SENT
    assert len(wire.calls) == 2


def test_a_result_without_a_recording_says_so() -> None:
    _, _, text = result_message(REPAIR, INCIDENT, PASSED_ATTEMPT)
    assert "recording: none published for this head" in text


def test_a_correction_is_keyed_by_its_own_wording(tmp_path: Path) -> None:
    """Corrections repair the record forward and never repeat themselves."""
    wire = Recorder()
    notifier = Notifier(log=_ledger(tmp_path), webhook=WEBHOOK, transport=wire)
    event_id, kind, text = correction_message("Integration correction: <it> was wrong")
    assert notifier.publish(event_id, kind, text) == SENT
    assert notifier.publish(event_id, kind, text) == SENT
    assert len(wire.calls) == 1
    assert "&lt;it&gt;" in wire.calls[0]["json"]["text"]
    assert correction_message("another correction")[0] != event_id


def test_a_result_is_refused_unless_the_attempt_measured_that_head() -> None:
    assert result_problem(REPAIR, PASSED_ATTEMPT) == ""
    # Another SHA's pass is another experiment.
    assert result_problem(REPAIR, PASSED_ATTEMPT | {"candidate_sha": "a" * 40})
    assert result_problem(REPAIR, PASSED_ATTEMPT | {"verdict": "blocked"})
    assert result_problem(REPAIR, None)
    assert result_problem(REPAIR | {"state": CANDIDATE}, PASSED_ATTEMPT)
    with pytest.raises(ValueError):
        result_message(REPAIR, INCIDENT, PASSED_ATTEMPT | {"candidate_sha": "a" * 40})


# --- what a block promises -------------------------------------------------


def test_a_block_reports_the_state_it_left_the_repair_in() -> None:
    blocked = REPAIR | {"state": DISPATCHED, "attention": "stack init exited 1"}
    _, _, retrying = message_for("blocked", blocked, INCIDENT)
    _, _, parked = message_for(
        "blocked",
        blocked | {"state": NEEDS_ATTENTION, "attention": "no runner capacity"},
        INCIDENT,
    )
    _, _, stopped = message_for(
        "blocked",
        blocked | {"state": TERMINAL, "terminal_reason": "deadline reached"},
        INCIDENT,
    )

    assert "the coordinator replays the same SHA" in retrying
    assert "parked for a human — no runner capacity" in parked
    assert "the repair is stopped — deadline reached" in stopped
    for text in (retrying, parked, stopped):
        assert "not completed" in text
