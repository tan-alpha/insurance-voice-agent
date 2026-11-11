"""Deterministic field validation, conflict resolution, and outcome evaluation.

Implements the 10 field rules (Section 2.1), the Conflict Resolution Matrix
(Section 2.2), and the Outcome & Revision State Machine (Section 2.3) of
@SPRINT_PLAN.md.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .protocol import (
    EngagementMetrics,
    FieldSource,
    FieldStatus,
    FinalRecord,
    InputCancel,
    InputKind,
    InputOption,
    InputRequest,
    RecordConflict,
    RecordField,
    Role,
    SessionState,
    Outcome,
)

KOLKATA = timezone(timedelta(hours=5, minutes=30))

VEHICLE_TYPE_OPTIONS = [
    {"value": "car", "label": "Car / SUV"},
    {"value": "twoWheeler", "label": "Two-Wheeler"},
    {"value": "commercial", "label": "Commercial Vehicle"},
]

VEHICLE_TYPE_MAP = {v["value"].lower(): v["value"] for v in VEHICLE_TYPE_OPTIONS}

POLICY_RE = re.compile(r"^POL-[0-9]{6}$")
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

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

STRUCTURED_FIELDS = {"policyNumber", "vehicleType", "email", "damagePhotos"}

CHAT_NATIVE_ORDER = ["policyNumber", "vehicleType", "email", "damagePhotos"]
SPOKEN_ORDER = [
    "incidentDescription",
    "incidentLocation",
    "incidentTime",
    "injuries",
    "vehicleDrivable",
]

EMAIL_INPUT_REQUEST = InputRequest(
    id="req_email",
    field="email",
    kind=InputKind.TEXT,
    label="Email for claim updates (optional)",
    required=False,
)


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _normalize_policy_number(value: str) -> str:
    v = value.strip().upper()
    v = v.replace(" ", "").replace("-", "")
    # Re-insert the single hyphen expected by the POL-XXXXXX format.
    if v.startswith("POL") and len(v) > 3:
        v = "POL-" + v[3:]
    return v


def _parse_incident_time(raw: str, now_iso: Optional[str]) -> Optional[str]:
    """Parse ISO-8601 or natural expressions into ISO-8601 +05:30.

    Supports relative offsets ('2 hours ago', 'two hours ago', 'about 30 minutes ago'),
    named periods ('today morning', 'yesterday evening', 'midnight'),
    clock times ('today at 9:30am', 'around 12 am', 'twelve am'), and ISO formats.
    Returns None when the value cannot be parsed or is out of range.
    """
    text = _collapse_whitespace(raw).strip()
    if not text:
        return None

    now: Optional[datetime] = None
    if now_iso:
        try:
            now = datetime.fromisoformat(now_iso)
        except ValueError:
            now = None
    if now is None:
        now = datetime.now(KOLKATA)

    candidate: Optional[datetime] = None
    lowered = text.lower()

    # ISO-8601 / "YYYY-MM-DD HH:MM".
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            candidate = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue

    if candidate is None:
        # Strip conversational leading filler phrases from spoken input.
        cleaned = re.sub(
            r"^(?:it\s+was\s+|happened\s+|occurred\s+|around\s+|about\s+|approx(?:\.|imately)?\s+|roughly\s+|maybe\s+|at\s+)+",
            "",
            lowered,
        ).strip()

        # Word numbers and common conversational time spans.
        word_map = {
            "half an hour ago": "30 minutes ago",
            "half a hour ago": "30 minutes ago",
            "an hour ago": "1 hour ago",
            "a hour ago": "1 hour ago",
            "a couple of hours ago": "2 hours ago",
            "a few hours ago": "3 hours ago",
            "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
            "eleven": "11", "twelve": "12", "midnight": "12 am", "noon": "12 pm",
        }
        for k, v in word_map.items():
            cleaned = re.sub(r"\b" + k + r"\b", v, cleaned)

        # "2 hours ago", "30 minutes ago", or embedded in phrase.
        m_ago = re.search(r"(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\s*ago", cleaned)
        if m_ago:
            n = int(m_ago.group(1))
            unit = m_ago.group(2)
            delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
            candidate = now - delta
        elif cleaned in ("today", "today morning", "this morning"):
            candidate = now.replace(hour=9, minute=0, second=0, microsecond=0)
        elif cleaned in ("today afternoon", "this afternoon"):
            candidate = now.replace(hour=15, minute=0, second=0, microsecond=0)
        elif cleaned in ("today evening", "this evening"):
            candidate = now.replace(hour=19, minute=0, second=0, microsecond=0)
        elif cleaned == "yesterday":
            candidate = (now - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif cleaned == "yesterday morning":
            candidate = (now - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif cleaned == "yesterday afternoon":
            candidate = (now - timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
        elif cleaned == "yesterday evening":
            candidate = (now - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
        else:
            # Matches "today at 9:30am", "at 12 am", "12 am", "9:30 am", etc.
            m_time = re.search(r"(?:today\s+at\s+|at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", cleaned)
            if m_time:
                hour = int(m_time.group(1))
                minute = int(m_time.group(2) or 0)
                ampm = m_time.group(3)
                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                # If parsed clock time is in the future compared to now, assume earlier today / yesterday
                if candidate > now + timedelta(minutes=5):
                    candidate -= timedelta(days=1)
            else:
                return None

    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=KOLKATA)

    if candidate > now + timedelta(minutes=5):
        return None  # future
    if candidate < now - timedelta(days=365):
        return None  # too far in the past

    return candidate.astimezone(KOLKATA).isoformat()


def _normalize_email(raw: str, source: FieldSource) -> str:
    v = _collapse_whitespace(raw).strip().lower()
    if source == FieldSource.CALL:
        v = v.replace(" at ", "@").replace(" dot ", ".")
        v = v.replace(" at", "@").replace(" dot", ".")
    return v


def validate_field(
    field: str,
    raw_value: Any,
    source: FieldSource,
    now_iso: Optional[str] = None,
    valid_upload_urls: Optional[set[str]] = None,
) -> tuple[bool, Any, Optional[str]]:
    """Validate and normalize a single field value.

    Returns ``(is_valid, normalized_value, error_message)``.
    """
    if field == "incidentDescription":
        v = _collapse_whitespace(str(raw_value)).strip()
        if len(v) >= 10 and len(v.split()) >= 2:
            return True, v, None
        return False, v, "incidentDescription must be at least 10 characters with 2+ words"

    if field == "incidentLocation":
        v = _collapse_whitespace(str(raw_value)).strip()
        if len(v) >= 3:
            return True, v, None
        return False, v, "incidentLocation must be at least 3 characters"

    if field == "incidentTime":
        normalized = _parse_incident_time(str(raw_value), now_iso)
        if normalized is None:
            return False, raw_value, "incidentTime could not be parsed or is out of range"
        return True, normalized, None

    if field == "injuries":
        v = _collapse_whitespace(str(raw_value)).strip().lower().rstrip(".!?,")
        none_tokens = {
            "no", "none", "no one hurt", "nobody", "nobody was hurt",
            "no one was hurt", "no injuries", "no injury", "not hurt",
            "everyone is fine", "nobody is hurt", "nil", "na", "n/a",
            "no bodily injury", "no one injured", "nobody injured",
        }
        if v in none_tokens:
            return True, "none", None
        if any(v.startswith(p) for p in ("no ", "no.", "no,", "none", "nobody", "no one", "no injury", "not injured")):
            if not any(inj in v for inj in ("broken", "bleed", "fracture", "pain", "hospital", "whiplash", "concussion", "hurt my", "injured my")):
                return True, "none", None
        if len(v) >= 3:
            return True, v, None
        return False, v, "injuries must be 'none' or describe the injury"

    if field == "vehicleDrivable":
        if isinstance(raw_value, bool):
            return True, raw_value, None
        v = _collapse_whitespace(str(raw_value)).strip().lower().rstrip(".!?,")
        true_tokens = {
            "yes", "true", "drivable", "car is drivable", "it is drivable",
            "it's drivable", "driveable", "yes drivable", "yes it is", "car works",
            "yes it is drivable",
        }
        false_tokens = {
            "no", "false", "not drivable", "towed", "undrivable",
            "not driveable", "cannot drive", "can't drive", "it was towed",
            "car is not drivable", "it is not drivable", "no it is not drivable",
        }
        if v in true_tokens or ("drivable" in v and "not" not in v and "un" not in v):
            return True, True, None
        if v in false_tokens or "not drivable" in v or "towed" in v:
            return True, False, None
        return False, v, "vehicleDrivable must be yes/no"

    if field == "policyNumber":
        v = _normalize_policy_number(str(raw_value))
        if POLICY_RE.match(v):
            return True, v, None
        return False, v, 'policyNumber must match POL- followed by 6 digits (e.g. POL-123456)'

    if field == "vehicleType":
        v = _collapse_whitespace(str(raw_value)).strip().lower()
        if v in VEHICLE_TYPE_MAP:
            return True, VEHICLE_TYPE_MAP[v], None
        return False, v, "vehicleType must be one of: car, twoWheeler, commercial"

    if field == "damagePhotos":
        if not isinstance(raw_value, list):
            return False, raw_value, "damagePhotos must be a list of 1-5 image URLs"
        if not (1 <= len(raw_value) <= 5):
            return False, raw_value, "damagePhotos must contain 1 to 5 images"
        urls = set(raw_value)
        allowed = valid_upload_urls or set()
        if not urls.issubset(allowed):
            return False, raw_value, "damagePhotos contains URLs not uploaded in this session"
        return True, list(raw_value), None

    if field == "email":
        v = _normalize_email(str(raw_value), source)
        if v in ("skip", "", "none"):
            return True, None, None
        if EMAIL_RE.match(v):
            return True, v, None
        return False, v, "email must be a valid email address"

    if field == "confirmSubmit":
        if isinstance(raw_value, bool):
            return True, raw_value, None
        v = _collapse_whitespace(str(raw_value)).strip().lower()
        if v in {"yes", "true", "confirm", "submit"}:
            return True, True, None
        if v in {"no", "false", "cancel", "opt out"}:
            return True, False, None
        return False, v, "confirmSubmit must be yes/no"

    return False, raw_value, f"unknown field {field}"


def _make_record_field(value: Any, source: FieldSource, status: FieldStatus,
                       now_iso: Optional[str]) -> RecordField:
    return RecordField(
        value=value,
        source=source,
        collected_at=now_iso,
        status=status,
    )


def _conflict_for(field: str, existing: RecordField, new_value: Any,
                  new_source: FieldSource) -> RecordConflict:
    if field in STRUCTURED_FIELDS:
        resolution = "chatValue"
        reason = "Typed or uploaded chat input preferred over spoken ASR transcript"
        winning_value = new_value if new_source == FieldSource.CHAT else existing.value
    else:
        resolution = "chatValue" if new_source == FieldSource.CHAT else "callValue"
        reason = "User explicitly updated value in subsequent channel turn"
        winning_value = new_value
    return RecordConflict(
        field=field,
        call_value=existing.value if existing.source == FieldSource.CALL else new_value,
        chat_value=existing.value if existing.source == FieldSource.CHAT else new_value,
        resolution=resolution,
        reason=reason,
    )


def apply_field_update(
    state: SessionState,
    field: str,
    raw_value: Any,
    source: FieldSource,
    now_iso: Optional[str] = None,
    valid_upload_urls: Optional[set[str]] = None,
) -> tuple[bool, Optional[str], Optional[InputCancel]]:
    """Apply a validated field update to session state.

    Returns ``(ok, error_message, input_cancel)``.  ``input_cancel`` is set
    when a spoken answer supersedes an open chat card for the same field.
    """
    ok, normalized, err = validate_field(
        field, raw_value, source, now_iso, valid_upload_urls
    )

    if not ok:
        state.failure_counts[field] = state.failure_counts.get(field, 0) + 1
        return False, err, None

    state.failure_counts[field] = 0
    now_iso = now_iso or datetime.now(KOLKATA).isoformat()

    existing = state.fields.get(field)
    input_cancel: Optional[InputCancel] = None

    if existing is not None and existing.status == FieldStatus.VALID and existing.source != source and normalized != existing.value:
        state.conflicts.append(_conflict_for(field, existing, normalized, source))

    if field == "injuries" and normalized != "none":
        state.injury_review_flag = True
    elif field == "injuries" and normalized == "none":
        state.injury_review_flag = False

    if field == "email" and normalized is None:
        # Optional email skipped: mark valid without a value.
        state.fields[field] = _make_record_field(None, source, FieldStatus.VALID, now_iso)
        state.updated_at = now_iso
        return True, None, None

    if field == "confirmSubmit" and normalized is False:
        state.confirm_rejections += 1
        state.fields[field] = _make_record_field(False, source, FieldStatus.MISSING, now_iso)
        state.updated_at = now_iso
        return True, None, None

    state.fields[field] = _make_record_field(normalized, source, FieldStatus.VALID, now_iso)
    state.updated_at = now_iso

    if source == FieldSource.CALL and state.open_request is not None and state.open_request.field == field:
        input_cancel = InputCancel(
            request_id=state.open_request.id,
            reason="given on the call",
        )
        state.open_request = None

    return True, None, input_cancel


def _build_card_spec(field: str) -> InputRequest:
    if field == "policyNumber":
        return InputRequest(
            id="req_policyNumber",
            field="policyNumber",
            kind=InputKind.TEXT,
            label="Policy number",
            help="Format: POL-123456",
            placeholder="POL-123456",
            required=True,
        )
    if field == "vehicleType":
        return InputRequest(
            id="req_vehicleType",
            field="vehicleType",
            kind=InputKind.SELECT,
            label="Which vehicle was involved?",
            options=[InputOption(**o) for o in VEHICLE_TYPE_OPTIONS],
            required=True,
        )
    if field == "damagePhotos":
        return InputRequest(
            id="req_damagePhotos",
            field="damagePhotos",
            kind=InputKind.FILE,
            label="Photos of the damage",
            help="One or more images",
            accept="image/*",
            multiple=True,
            required=True,
        )
    if field == "email":
        return EMAIL_INPUT_REQUEST
    if field == "confirmSubmit":
        return InputRequest(
            id="req_confirmSubmit",
            field="confirmSubmit",
            kind=InputKind.CONFIRM,
            label="Submit the claim now?",
            required=True,
        )
    # Spoken fields are handled by the voice agent, not a chat card.
    return InputRequest(
        id=f"req_{field}",
        field=field,
        kind=InputKind.TEXT,
        label=field,
        required=True,
    )


def next_missing_input_request(
    state: SessionState,
    call_active: bool,
) -> Optional[InputRequest]:
    """Return the next InputRequest to dispatch, or None.

    When a call is active, only chat-native cards are queued, and only once
    the spoken fields (1-5) are all valid.  When there is no active call,
    spoken fields (1-5) are asked first, then chat cards (6-9), then
    confirmSubmit (10).
    """
    def missing(f: str) -> bool:
        rf = state.fields.get(f)
        return rf is None or rf.status != FieldStatus.VALID

    spoken_missing = [f for f in SPOKEN_ORDER if missing(f)]
    email_missing = missing("email")
    confirm_missing = missing("confirmSubmit")

    all_spoken_valid = not spoken_missing
    all_required_valid = all(not missing(f) for f in REQUIRED_FIELDS)

    if call_active:
        if not all_spoken_valid:
            return None
        for f in CHAT_NATIVE_ORDER:
            if f == "email":
                if email_missing and not state.email_prompted:
                    return _build_card_spec(f)
                continue
            if missing(f):
                return _build_card_spec(f)
        if all_required_valid and not email_missing and confirm_missing:
            return _build_card_spec("confirmSubmit")
        if all_required_valid and email_missing and state.email_prompted and confirm_missing:
            return _build_card_spec("confirmSubmit")
        return None

    # No active call: spoken first, then chat cards, then confirm.
    for f in SPOKEN_ORDER:
        if missing(f):
            return _build_card_spec(f)
    for f in CHAT_NATIVE_ORDER:
        if missing(f):
            return _build_card_spec(f)
    if all_required_valid and confirm_missing:
        return _build_card_spec("confirmSubmit")
    return None


def _build_record(state: SessionState, outcome: Outcome,
                  reason: str, now_iso: Optional[str] = None) -> FinalRecord:
    now_iso = now_iso or datetime.now(KOLKATA).isoformat()
    state.revision += 1
    state.outcome = outcome
    state.outcome_reason = reason
    state.ended_at = now_iso
    state.updated_at = now_iso

    engagement = EngagementMetrics(
        calls=state.engagement.calls,
        call_seconds=state.engagement.call_seconds,
        chat_messages=state.engagement.chat_messages,
    )

    fields_dict = dict(state.fields)
    for f in REQUIRED_FIELDS + ["email"]:
        if f not in fields_dict:
            if state.failure_counts.get(f, 0) > 0:
                fields_dict[f] = RecordField(
                    value=None,
                    source=None,
                    collected_at=now_iso,
                    status=FieldStatus.INVALID,
                )
            else:
                fields_dict[f] = RecordField(
                    value=None,
                    source=None,
                    collected_at=None,
                    status=FieldStatus.MISSING,
                )

    return FinalRecord(
        session_id=state.session_id,
        revision=state.revision,
        outcome=outcome,
        outcome_reason=reason,
        fields=fields_dict,
        conflicts=list(state.conflicts) if state.conflicts else None,
        engagement=engagement,
        cost_usd=state.cost_usd if state.cost_usd else None,
        ended_at=now_iso,
    )


def evaluate_outcome(
    state: SessionState,
    force_partial: bool = False,
    opt_out_reason: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> Optional[FinalRecord]:
    """Evaluate the terminal-outcome state machine.

    Returns a ``FinalRecord`` when a terminal outcome is reached, else None.
    """
    now_iso = now_iso or datetime.now(KOLKATA).isoformat()

    if opt_out_reason:
        return _build_record(state, Outcome.OPTED_OUT, opt_out_reason, now_iso)

    if state.confirm_rejections >= 2:
        return _build_record(
            state, Outcome.OPTED_OUT,
            "User rejected final confirmation twice",
            now_iso,
        )

    # Immediate trigger: 3 consecutive validation failures on any field.
    for field, count in state.failure_counts.items():
        if count >= 3:
            return _build_record(
                state, Outcome.NEEDS_REVIEW,
                f"{field} failed validation {count} consecutive times",
                now_iso,
            )

    rf_confirm = state.fields.get("confirmSubmit")
    confirm_ok = (
        rf_confirm is not None
        and rf_confirm.status == FieldStatus.VALID
        and rf_confirm.value is True
    )
    all_required_valid = all(
        state.fields.get(f) is not None
        and state.fields.get(f).status == FieldStatus.VALID
        for f in REQUIRED_FIELDS
    )
    email_rf = state.fields.get("email")
    email_ok = email_rf is None or email_rf.status != FieldStatus.INVALID

    # Post-confirm bodily-injury needsReview.
    if all_required_valid and confirm_ok and email_ok and state.injury_review_flag:
        return _build_record(
            state, Outcome.NEEDS_REVIEW,
            "Bodily injury reported; requires human adjuster review",
            now_iso,
        )

    if all_required_valid and confirm_ok and email_ok and state.vision_review_flag:
        return _build_record(
            state,
            Outcome.NEEDS_REVIEW,
            "Uploaded photo flagged by AI vision for human adjuster review",
            now_iso,
        )

    # Completed.
    if all_required_valid and confirm_ok and email_ok:
        return _build_record(
            state, Outcome.COMPLETED,
            "All required claim fields validated and confirmed by user",
            now_iso,
        )

    # Partial (watchdog-driven).
    if force_partial:
        any_missing_or_invalid = any(
            state.fields.get(f) is None
            or state.fields.get(f).status != FieldStatus.VALID
            for f in REQUIRED_FIELDS
        ) or not confirm_ok
        if any_missing_or_invalid:
            return _build_record(
                state, Outcome.PARTIAL,
                "user stopped responding with required fields missing or invalid",
                now_iso,
            )

    return None
