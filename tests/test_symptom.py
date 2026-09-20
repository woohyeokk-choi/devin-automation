"""The two recordings an incident is worth, at the seam that produces them.

One shows the failure as the baseline still produces it, asked for while the
repair runs; the other shows the merged commit running. They are evidence
about different code, so what is asserted here is mostly that they cannot be
swapped: a clip of the old failure must never arrive looking like a fix, and
the after-merge gate must not be reachable by footage taken before it.

No credentials and no network: the same fakes the rest of the suite uses.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from portal.controller import (
    AFTER_MERGE,
    MEDIA_DELIVERED,
    MEDIA_FAILED,
    MEDIA_PENDING,
    MEDIA_REQUESTED,
    MERGED,
    SYMPTOM,
    RepairStore,
)
from portal.verification import VerificationStore

from test_controller import Wiring, _candidate, second_incident
from test_merge_gate import (
    CLOCK,
    _mark_real,
    capture_name,
    gated,
    merge,
    previewed,
    stamp,
    verifier_for,
    worker_over,
    env_at,
    MERGE_SHA,
)
from test_verification import CANDIDATE_SHA, FakeRunner

BASELINE = "394bca55c792b7b3547e23f6e175a7cb0f0757e8"


@pytest.fixture()
def store(tmp_path: Path) -> VerificationStore:
    return VerificationStore(tmp_path / "simulated-verifications.sqlite")


def symptom_name(sha: str = BASELINE, case: str = "S2", at: str = "") -> str:
    # The shared incident fixture is the S2 family; N1 is only its control.
    return f"symptom-{sha}-{at or stamp()}-{case}.mp4"


def fake_download(url: str, destination: Path, **kwargs: Any) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"simulated capture")
    return destination


def dispatched(
    wiring: Wiring, incident: dict[str, Any], tmp_path: Path
) -> int:
    """A repair whose session is running and owes the symptom clip."""
    wiring.controller.media_dir = tmp_path / "media"
    decision = wiring.controller.consider(incident)
    wiring.incidents[int(incident["id"])] = incident
    repair_id = decision.repair_id or 0
    for taken in wiring.controller.advance():
        if taken.repair_id == repair_id:
            break
    return repair_id


# --- the symptom is asked for while the failure still exists ---------------


def test_dispatch_owes_a_symptom_clip_of_the_baseline(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore, tmp_path: Path
) -> None:
    repair_id = dispatched(wiring, incident, tmp_path)

    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_stage"] == SYMPTOM
    assert repair["media_state"] == MEDIA_PENDING


def test_the_session_is_asked_once_for_footage_of_the_baseline(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore, tmp_path: Path
) -> None:
    repair_id = dispatched(wiring, incident, tmp_path)

    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    session_id, message = wiring.devin_api.messages[0]
    assert session_id == wiring.session_id()
    assert BASELINE in message and f"symptom-{BASELINE}" in message
    assert "before you change any product code" in message

    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_REQUESTED

    # A restart, or simply the next pass, says nothing twice.
    wiring.controller.check_media(repair_id)
    wiring.controller.advance()
    assert len(wiring.devin_api.messages) == 1


def test_a_clip_of_anything_but_the_baseline_is_not_the_symptom(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore, tmp_path: Path
) -> None:
    repair_id = dispatched(wiring, incident, tmp_path)
    wiring.controller.check_media(repair_id)
    session = wiring.session_id()
    # Footage of the candidate, footage a person uploaded, another case, and
    # a post-merge clip that names the right commit but the wrong stage.
    wiring.devin_api.add_attachment(session, symptom_name(sha=CANDIDATE_SHA))
    wiring.devin_api.add_attachment(session, symptom_name(), source="user")
    wiring.devin_api.add_attachment(session, symptom_name(case="N1"))
    wiring.devin_api.add_attachment(session, capture_name(sha=BASELINE))

    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    repair = repairs.get(repair_id)
    assert repair is not None and not repair["media_path"]


def test_the_symptom_clip_reaches_slack_without_ending_the_repair(
    wiring: Wiring,
    incident: dict[str, Any],
    repairs: RepairStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_slack_bot import FakeBot

    repair_id = dispatched(wiring, incident, tmp_path)
    wiring.controller.check_media(repair_id)
    wiring.devin_api.add_attachment(wiring.session_id(), symptom_name())
    monkeypatch.setattr("portal.media.download", fake_download)

    assert wiring.controller.check_media(repair_id).action == "media_captured"
    # What is under test is how the symptom is published, not the refusal of
    # scripted evidence, which `test_slack_bot` covers.
    wiring.controller.store.simulated = False
    wiring.controller.store.update(repair_id, simulated=0)
    bot = FakeBot("F0CSYMPTOM")
    worker = worker_over(wiring, tmp_path, bot)
    worker._publish_capture(repair_id)

    assert len(bot.uploads) == 1
    comment = bot.uploads[0].get("comment", "")
    assert "Symptom replay — recorded after detection" in comment
    assert BASELINE[:12] in comment
    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_state"] == MEDIA_DELIVERED
    assert repair["media_file_id"] == "F0CSYMPTOM"
    # The repair is still running: its session was not stopped and its claim
    # was not handed to anybody else.
    assert not wiring.devin_api.terminated
    assert repairs.slot_holder() == repair_id

    # The ledger, not the caller, is what stops a second upload.
    worker._publish_capture(repair_id)
    assert len(bot.uploads) == 1


def test_a_worker_pass_publishes_the_symptom_while_the_repair_moves_on(
    wiring: Wiring,
    incident: dict[str, Any],
    repairs: RepairStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody calls the upload by hand in production: a pass has to do it.

    The repair's own progress is what a pass is normally about, so the
    recording's arrival has to survive being carried alongside it.
    """
    from test_slack_bot import FakeBot

    repair_id = dispatched(wiring, incident, tmp_path)
    wiring.controller.store.simulated = False
    wiring.controller.store.update(repair_id, simulated=0)
    monkeypatch.setattr("portal.media.download", fake_download)
    bot = FakeBot("F0CSYMPTOM")
    worker = worker_over(wiring, tmp_path, bot)

    worker.tick()  # asks for the recording
    wiring.devin_api.add_attachment(wiring.session_id(), symptom_name())
    decisions = worker.tick()  # finds it, fetches it, and offers it

    assert [d.action for d in decisions if d.action == "media_captured"]
    assert len(bot.uploads) == 1
    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_state"] == MEDIA_DELIVERED
    assert repair["media_file_id"] == "F0CSYMPTOM"
    # The repair itself kept moving, and nothing is offered a second time.
    assert repair["state"] not in ("", None)
    worker.tick()
    assert len(bot.uploads) == 1


def test_a_symptom_upload_that_fails_is_visible(
    wiring: Wiring,
    incident: dict[str, Any],
    repairs: RepairStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repair_id = dispatched(wiring, incident, tmp_path)
    wiring.controller.check_media(repair_id)
    wiring.devin_api.add_attachment(wiring.session_id(), symptom_name())
    monkeypatch.setattr("portal.media.download", fake_download)
    wiring.controller.check_media(repair_id)
    wiring.controller.store.simulated = False
    wiring.controller.store.update(repair_id, simulated=0)

    # No bot at all: a webhook cannot upload a file.
    worker_over(wiring, tmp_path, None)._publish_capture(repair_id)

    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_FAILED
    assert repair["media_detail"]
    assert not wiring.devin_api.terminated


def test_a_missing_symptom_clip_fails_loudly_rather_than_disappearing(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore, tmp_path: Path
) -> None:
    repair_id = dispatched(wiring, incident, tmp_path)
    wiring.controller.check_media(repair_id)
    # The session runs past the wall clock without ever attaching anything.
    wiring.clock = CLOCK + timedelta(hours=4)

    assert wiring.controller.check_media(repair_id).action == "media_failed"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_FAILED


# --- and the merged clip is a separate promise -----------------------------


def test_the_merged_recording_is_asked_for_separately_from_the_symptom(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_slack_bot import FakeBot

    wiring = gated(repairs)
    wiring.controller.media_dir = tmp_path / "media"
    monkeypatch.setattr("portal.media.download", fake_download)

    # The symptom, delivered while the repair is still being worked on.
    repair_id = _candidate(wiring, incident)
    wiring.controller.check_media(repair_id)
    wiring.devin_api.add_attachment(wiring.session_id(), symptom_name())
    assert wiring.controller.check_media(repair_id).action == "media_captured"
    _mark_real(wiring, repair_id, store)
    bot = FakeBot("F0CSYMPTOM")
    worker = worker_over(wiring, tmp_path, bot)
    worker._publish_capture(repair_id)

    # Then the preview, the human's merge, and the merged commit's own clip.
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(CANDIDATE_SHA))
    )
    wiring.controller.verify(repair_id)
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(MERGE_SHA))
    )
    merge(wiring)
    assert wiring.controller.check_merge(repair_id).action == "merge_verified"

    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_stage"] == AFTER_MERGE
    assert repair["media_state"] == MEDIA_PENDING
    # The symptom's file is not carried forward as the merged commit's.
    assert not repair["media_path"] and not repair["media_file_id"]

    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    assert f"post-merge-{MERGE_SHA}" in wiring.devin_api.messages[-1][1]
    # The symptom clip cannot answer the merged request.
    wiring.devin_api.add_attachment(wiring.session_id(), symptom_name(sha=MERGE_SHA))
    assert wiring.controller.check_media(repair_id).action == "awaiting_media"

    wiring.devin_api.add_attachment(wiring.session_id(), capture_name())
    assert wiring.controller.check_media(repair_id).action == "media_captured"
    _mark_real(wiring, repair_id, store)
    worker._publish_capture(repair_id)

    assert len(bot.uploads) == 2
    assert "After merge" not in bot.uploads[0].get("comment", "")
    assert "After merge — verified" in bot.uploads[1].get("comment", "")
    assert "Symptom replay" not in bot.uploads[1].get("comment", "")
    assert MERGE_SHA[:12] in bot.uploads[1].get("comment", "")
    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["state"] == MERGED and repair["media_state"] == MEDIA_DELIVERED
    # This one does end the session: it was the last thing it was kept for.
    assert wiring.devin_api.terminated == [wiring.session_id()]


def test_preview_footage_cannot_stand_in_for_the_merged_commit(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    wiring = gated(repairs)
    wiring.controller.media_dir = tmp_path / "media"
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(MERGE_SHA))
    )
    merge(wiring)
    wiring.controller.check_merge(repair_id)
    wiring.controller.check_media(repair_id)
    wiring.devin_api.add_attachment(wiring.session_id(), capture_name(sha=CANDIDATE_SHA))

    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_state"] == MEDIA_REQUESTED and not repair["media_path"]


# --- which case a recording must be of ------------------------------------


def test_the_recording_asked_for_is_the_defect_case_not_the_control(
    wiring: Wiring, incident: dict[str, Any], tmp_path: Path
) -> None:
    """S2+N1 and S1+N1 both register a control, and neither is the defect."""
    repair_id = dispatched(wiring, incident, tmp_path)
    wiring.controller.check_media(repair_id)

    assert incident["family"] == "discarded_form_data_key_is_reused"
    message = wiring.devin_api.messages[0][1]
    assert "-S2.mp4" in message and "-N1.mp4" not in message


def test_the_row_limit_family_is_recorded_as_s1(
    wiring: Wiring, repairs: RepairStore, tmp_path: Path
) -> None:
    s1 = second_incident(tmp_path)
    repair_id = dispatched(wiring, s1, tmp_path)
    wiring.controller.check_media(repair_id)

    assert s1["family"] == "omitted_row_limit_is_reset"
    message = wiring.devin_api.messages[0][1]
    assert "-S1.mp4" in message and "-N1.mp4" not in message

    # And a clip of the control, of the right commit at the right time, is
    # still not footage of the defect.
    wiring.devin_api.add_attachment(
        wiring.session_id(), symptom_name(sha=str(s1["baseline_sha"]), case="N1")
    )
    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    repair = repairs.get(repair_id)
    assert repair is not None and not repair["media_path"]


def test_a_family_with_no_single_defect_case_records_nothing(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore, tmp_path: Path
) -> None:
    """Guessing a case would let any clip through, so nothing is guessed."""
    unknown = dict(incident)
    unknown["family"] = "a_family_nobody_registered"
    repair_id = dispatched(wiring, unknown, tmp_path)
    wiring.incidents[int(unknown["id"])] = unknown

    assert wiring.controller.check_media(repair_id).action == "media_failed"
    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_state"] == MEDIA_FAILED
    assert "one defect case" in str(repair["media_detail"])
    assert not wiring.devin_api.messages
