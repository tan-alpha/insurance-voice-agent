"""Sprint 1 acceptance-gate tests.

Covers backend/tests/test_sprint1.py acceptance criteria from @SPRINT_PLAN.md:
  a) Pydantic serialization matches camelCase JSON without `pattern`.
  b) All 10 field validators tested with valid & invalid inputs.
  c) Conflict resolution (chat overrides call for structured fields).
  d) 3 consecutive validation failures on `policyNumber` -> needsReview (revision=1).
  e) Bodily injury (`injuries != "none"`) -> needsReview.
  f) SQLite store crash recovery + idempotent `mark_revision_delivered`.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from backend.app.protocol import (
    AudioMessage,
    ChatMessage,
    FieldSource,
    FieldStatus,
    InputCancel,
    InputKind,
    InputRequest,
    Outcome,
    RecordMessage,
    Role,
    SessionState,
    TranscriptMessage,
    FinalRecord,
)
from backend.app.validator import (
    apply_field_update,
    evaluate_outcome,
    next_missing_input_request,
    validate_field,
)
from backend.app.store import SessionStore

KOLKATA_ISO = "2026-11-10T19:15:00+05:30"

REQUIRED_FIELDS = [
    "incidentDescription",
    "incidentLocation",
    "incidentTime",
    "injuries",
    "vehicleDrivable",
    "policyNumber",
    "vehicleType",
    "damagePhotos",
]


# ---------------------------------------------------------------------------
# (a) Serialization matches camelCase keys, `pattern` omitted
# ---------------------------------------------------------------------------

def test_input_request_serialization_omits_pattern():
    req = InputRequest(
        id="req1",
        field="policyNumber",
        kind=InputKind.TEXT,
        label="Policy number",
        required=True,
    )
    payload = json.loads(
        req.model_dump_json(by_alias=True, exclude_none=True)
    )
    assert payload["type"] == "inputRequest"
    assert payload["id"] == "req1"
    assert payload["field"] == "policyNumber"
    assert payload["kind"] == "text"
    assert payload["label"] == "Policy number"
    assert payload["required"] is True
    assert "pattern" not in payload


def test_transcript_message_serialization():
    msg = TranscriptMessage(role=Role.AGENT, text="hello", final=True, id="t1")
    payload = json.loads(msg.model_dump_json(by_alias=True, exclude_none=True))
    assert payload["type"] == "transcript"
    assert payload["role"] == "agent"
    assert payload["text"] == "hello"
    assert payload["final"] is True
    assert payload["id"] == "t1"


def test_audio_message_serialization():
    msg = AudioMessage(data="base64data")
    payload = json.loads(msg.model_dump_json(by_alias=True, exclude_none=True))
    assert payload["type"] == "audio"
    assert payload["data"] == "base64data"
    assert "sampleRate" not in payload


def test_record_message_serialization():
    record = FinalRecord(
        session_id="s1",
        revision=1,
        outcome=Outcome.COMPLETED,
        outcome_reason="done",
        fields={},
    )
    msg = RecordMessage(record=record)
    payload = json.loads(msg.model_dump_json(by_alias=True, exclude_none=True))
    assert payload["type"] == "record"
    assert payload["record"]["sessionId"] == "s1"
    assert payload["record"]["revision"] == 1
    assert payload["record"]["outcome"] == "completed"
    assert payload["record"]["outcomeReason"] == "done"


# ---------------------------------------------------------------------------
# (b) All 10 field validators: valid & invalid
# ---------------------------------------------------------------------------

VALID_CASES = [
    ("incidentDescription", "Rear-ended by a truck at a signal", FieldSource.CALL, True),
    ("incidentDescription", "short", FieldSource.CALL, False),
    ("incidentLocation", "MG Road, near Trinity metro", FieldSource.CALL, True),
    ("incidentLocation", "ab", FieldSource.CALL, False),
    ("incidentTime", "today at 9:30am", FieldSource.CALL, True),
    ("incidentTime", "2 hours ago", FieldSource.CALL, True),
    ("incidentTime", "two hours ago", FieldSource.CALL, True),
    ("incidentTime", "about two hours ago", FieldSource.CALL, True),
    ("incidentTime", "around two hours ago", FieldSource.CALL, True),
    ("incidentTime", "2099-01-01 10:00", FieldSource.CALL, False),
    ("incidentTime", "not a date", FieldSource.CALL, False),
    ("injuries", "no one hurt", FieldSource.CALL, True),
    ("injuries", "nobody was hurt", FieldSource.CALL, True),
    ("injuries", "whiplash in neck", FieldSource.CALL, True),
    ("injuries", "ow", FieldSource.CALL, False),
    ("vehicleDrivable", True, FieldSource.CALL, True),
    ("vehicleDrivable", "yes", FieldSource.CALL, True),
    ("vehicleDrivable", "towed", FieldSource.CALL, True),
    ("vehicleDrivable", "the car is drivable", FieldSource.CALL, True),
    ("vehicleDrivable", "maybe", FieldSource.CALL, False),
    ("policyNumber", "POL-123456", FieldSource.CHAT, True),
    ("policyNumber", "POL-1234567", FieldSource.CHAT, False),
    ("policyNumber", "POL-123", FieldSource.CHAT, False),
    ("vehicleType", "Car", FieldSource.CHAT, True),
    ("vehicleType", "twoWheeler", FieldSource.CHAT, True),
    ("vehicleType", "commercial", FieldSource.CHAT, True),
    ("vehicleType", "bike", FieldSource.CHAT, False),
    ("damagePhotos", ["http://x/1.png"], FieldSource.CHAT, True),
    ("damagePhotos", [], FieldSource.CHAT, False),
    ("damagePhotos", ["http://x/1.png"] * 6, FieldSource.CHAT, False),
    ("email", "john at example dot com", FieldSource.CALL, True),
    ("email", "skip", FieldSource.CHAT, True),
    ("email", "", FieldSource.CHAT, True),
    ("email", "not-an-email", FieldSource.CHAT, False),
    ("confirmSubmit", True, FieldSource.CHAT, True),
    ("confirmSubmit", "no", FieldSource.CHAT, True),
    ("confirmSubmit", "maybe", FieldSource.CHAT, False),
]


@pytest.mark.parametrize("field,raw,source,expect_valid", VALID_CASES)
def test_all_field_validators(field, raw, source, expect_valid):
    kwargs = {}
    if field == "damagePhotos" and isinstance(raw, list):
        kwargs["valid_upload_urls"] = set(raw)
    ok, normalized, err = validate_field(field, raw, source, KOLKATA_ISO, **kwargs)
    assert ok is expect_valid, f"{field}={raw!r}: expected {expect_valid}, got {ok} err={err}"
    if expect_valid:
        assert err is None


def test_damagePhotos_requires_session_urls():
    ok, _, _ = validate_field(
        "damagePhotos", ["http://x/1.png"], FieldSource.CHAT,
        KOLKATA_ISO, valid_upload_urls={"http://x/other.png"},
    )
    assert ok is False

    ok, _, _ = validate_field(
        "damagePhotos", ["http://x/1.png"], FieldSource.CHAT,
        KOLKATA_ISO, valid_upload_urls={"http://x/1.png"},
    )
    assert ok is True


# ---------------------------------------------------------------------------
# (c) Conflict resolution: chat overrides call for structured fields
# ---------------------------------------------------------------------------

def test_structured_conflict_chat_wins():
    state = SessionState(session_id="s_conflict", created_at=KOLKATA_ISO, updated_at=KOLKATA_ISO)
    ok, _, _ = apply_field_update(state, "policyNumber", "POL-111111", FieldSource.CALL, KOLKATA_ISO)
    assert ok is True
    assert state.fields["policyNumber"].value == "POL-111111"
    assert state.fields["policyNumber"].source == FieldSource.CALL

    ok, _, cancel = apply_field_update(state, "policyNumber", "POL-222222", FieldSource.CHAT, KOLKATA_ISO)
    assert ok is True
    assert state.fields["policyNumber"].value == "POL-222222"
    assert state.fields["policyNumber"].source == FieldSource.CHAT
    assert len(state.conflicts) == 1
    conflict = state.conflicts[0]
    assert conflict.field == "policyNumber"
    assert conflict.call_value == "POL-111111"
    assert conflict.chat_value == "POL-222222"
    assert conflict.resolution == "chatValue"


# ---------------------------------------------------------------------------
# (d) 3 consecutive invalid policyNumber -> needsReview, revision=1
# ---------------------------------------------------------------------------

def test_three_strikes_policyNumber_needsReview():
    state = SessionState(session_id="s_strike", created_at=KOLKATA_ISO, updated_at=KOLKATA_ISO)
    for i in range(3):
        ok, err, _ = apply_field_update(state, "policyNumber", "bad", FieldSource.CHAT, KOLKATA_ISO)
        assert ok is False
        assert err is not None
    assert state.failure_counts["policyNumber"] == 3

    record = evaluate_outcome(state, now_iso=KOLKATA_ISO)
    assert record is not None
    assert record.outcome == Outcome.NEEDS_REVIEW
    assert record.revision == 1
    assert state.revision == 1
    # Verify missing & invalid fields are properly structured in record.fields
    assert record.fields["policyNumber"].status == FieldStatus.INVALID
    assert record.fields["policyNumber"].value is None
    assert record.fields["incidentDescription"].status == FieldStatus.MISSING
    assert record.fields["incidentDescription"].value is None
    assert record.fields["email"].status == FieldStatus.MISSING


# ---------------------------------------------------------------------------
# (e) Bodily injury -> needsReview (post-confirm)
# ---------------------------------------------------------------------------

def _all_required_valid(state, source=FieldSource.CALL):
    apply_field_update(state, "incidentDescription", "Rear-ended by a truck at a signal", source, KOLKATA_ISO)
    apply_field_update(state, "incidentLocation", "MG Road", source, KOLKATA_ISO)
    apply_field_update(state, "incidentTime", "2 hours ago", source, KOLKATA_ISO)
    apply_field_update(state, "injuries", "whiplash in neck", source, KOLKATA_ISO)
    apply_field_update(state, "vehicleDrivable", True, source, KOLKATA_ISO)
    apply_field_update(state, "policyNumber", "POL-123456", FieldSource.CHAT, KOLKATA_ISO)
    apply_field_update(state, "vehicleType", "car", FieldSource.CHAT, KOLKATA_ISO)
    apply_field_update(
        state, "damagePhotos", ["http://x/1.png"], FieldSource.CHAT,
        KOLKATA_ISO, valid_upload_urls={"http://x/1.png"},
    )


def test_bodily_injury_needsReview():
    state = SessionState(session_id="s_injury", created_at=KOLKATA_ISO, updated_at=KOLKATA_ISO)
    _all_required_valid(state)
    apply_field_update(state, "confirmSubmit", True, FieldSource.CHAT, KOLKATA_ISO)

    record = evaluate_outcome(state, now_iso=KOLKATA_ISO)
    assert record is not None
    assert record.outcome == Outcome.NEEDS_REVIEW
    assert "injury" in record.outcome_reason.lower()
    assert state.injury_review_flag is True


def test_clean_completion():
    state = SessionState(session_id="s_complete", created_at=KOLKATA_ISO, updated_at=KOLKATA_ISO)
    _all_required_valid(state)
    apply_field_update(state, "injuries", "none", FieldSource.CALL, KOLKATA_ISO)
    apply_field_update(state, "confirmSubmit", True, FieldSource.CHAT, KOLKATA_ISO)

    record = evaluate_outcome(state, now_iso=KOLKATA_ISO)
    assert record is not None
    assert record.outcome == Outcome.COMPLETED
    assert state.injury_review_flag is False


# ---------------------------------------------------------------------------
# (f) SQLite store crash recovery + idempotent mark_revision_delivered
# ---------------------------------------------------------------------------

def test_store_crash_recovery_and_delivery_idempotency():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SessionStore(path)
        state = store.get_or_create_session("s_recover")
        state.revision = 1
        state.outcome = Outcome.PARTIAL
        state.outcome_reason = "inactivity"
        store.save_session(state)
        store.close()

        # Re-open against the same file: state must survive a restart.
        store2 = SessionStore(path)
        recovered = store2.get_or_create_session("s_recover")
        assert recovered.revision == 1
        assert recovered.outcome == Outcome.PARTIAL
        assert recovered.delivered_revision == 0

        # Idempotent delivery: delivering revision 1 twice keeps delivered_revision == 1.
        store2.mark_revision_delivered("s_recover", 1)
        store2.mark_revision_delivered("s_recover", 1)
        again = store2.get_or_create_session("s_recover")
        assert again.delivered_revision == 1
        store2.close()
    finally:
        os.unlink(path)


def test_timeline_append_and_read():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SessionStore(path)
        store.get_or_create_session("s_tl")
        store.append_event("s_tl", "chat", "inputResponse", {"field": "policyNumber", "value": "POL-123456"})
        store.append_event("s_tl", "call", "transcript", {"text": "hello", "final": True})
        timeline = store.get_timeline("s_tl")
        assert len(timeline) == 2
        assert timeline[0]["channel"] == "chat"
        assert timeline[1]["channel"] == "call"
        assert timeline[1]["payload"]["text"] == "hello"
        store.close()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# next_missing_input_request progression
# ---------------------------------------------------------------------------

def test_next_missing_input_request_call_active_queues_chat_cards():
    state = SessionState(session_id="s_next", created_at=KOLKATA_ISO, updated_at=KOLKATA_ISO)
    _all_required_valid(state)
    apply_field_update(state, "email", "skip", FieldSource.CHAT, KOLKATA_ISO)
    apply_field_update(state, "confirmSubmit", True, FieldSource.CHAT, KOLKATA_ISO)

    # With an active call and everything complete, no further card.
    assert next_missing_input_request(state, call_active=True) is None

    # Without a call, confirm already valid -> None.
    assert next_missing_input_request(state, call_active=False) is None

    # Fresh state, no call: asks first spoken field.
    fresh = SessionState(session_id="s_fresh", created_at=KOLKATA_ISO, updated_at=KOLKATA_ISO)
    req = next_missing_input_request(fresh, call_active=False)
    assert req is not None
    assert req.field == "incidentDescription"

    # Fresh state, active call: spoken fields incomplete -> no chat card.
    assert next_missing_input_request(fresh, call_active=True) is None
