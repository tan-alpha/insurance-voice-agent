"""Wire protocol and domain models.

All Pydantic v2 models use ``ConfigDict(populate_by_name=True, extra="forbid")``
and are serialized by callers with ``by_alias=True, exclude_none=True`` so JSON
keys are camelCase (e.g. ``requestId``, ``sampleRate``, ``callValue``,
``costUsd``).  ``InputRequest.pattern`` defaults to ``None`` so client-side
blocking is bypassed and the backend validator owns validation errors.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums (mirroring starter/src/protocol/enums.ts)
# ---------------------------------------------------------------------------

class Role(str, Enum):
    USER = "user"
    AGENT = "agent"


class CallServerMessageType(str, Enum):
    AUDIO = "audio"
    STOP_PLAYBACK = "stopPlayback"
    TRANSCRIPT = "transcript"


class ChatClientMessageType(str, Enum):
    INPUT_RESPONSE = "inputResponse"


class ChatServerMessageType(str, Enum):
    MESSAGE = "message"
    INPUT_REQUEST = "inputRequest"
    INPUT_ERROR = "inputError"
    INPUT_CANCEL = "inputCancel"
    RECORD = "record"


class InputKind(str, Enum):
    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    NUMBER = "number"
    DATE = "date"
    SELECT = "select"
    MULTI_SELECT = "multiSelect"
    FILE = "file"
    CONFIRM = "confirm"


class Outcome(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    OPTED_OUT = "optedOut"
    NEEDS_REVIEW = "needsReview"


class FieldSource(str, Enum):
    CALL = "call"
    CHAT = "chat"


class FieldStatus(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"


# ---------------------------------------------------------------------------
# Base model config
# ---------------------------------------------------------------------------

class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# ---------------------------------------------------------------------------
# Server -> Client messages
# ---------------------------------------------------------------------------

class AudioMessage(_Base):
    type: CallServerMessageType = Field(default=CallServerMessageType.AUDIO)
    sample_rate: Optional[int] = Field(default=None, alias="sampleRate")
    data: str


class StopPlaybackMessage(_Base):
    type: CallServerMessageType = Field(default=CallServerMessageType.STOP_PLAYBACK)


class TranscriptMessage(_Base):
    type: CallServerMessageType = Field(default=CallServerMessageType.TRANSCRIPT)
    role: Role
    text: str
    final: bool
    id: Optional[str] = None


class ChatMessage(_Base):
    type: ChatServerMessageType = Field(default=ChatServerMessageType.MESSAGE)
    role: Role
    text: str
    id: Optional[str] = None


class InputOption(_Base):
    value: str
    label: Optional[str] = None


class InputRequest(_Base):
    type: ChatServerMessageType = Field(default=ChatServerMessageType.INPUT_REQUEST)
    id: str
    field: str
    kind: InputKind
    label: str
    help: Optional[str] = None
    placeholder: Optional[str] = None
    required: Optional[bool] = None
    pattern: Optional[str] = Field(default=None)
    options: Optional[list[InputOption]] = None
    accept: Optional[str] = None
    multiple: Optional[bool] = None


class InputError(_Base):
    type: ChatServerMessageType = Field(default=ChatServerMessageType.INPUT_ERROR)
    request_id: str = Field(alias="requestId")
    message: Optional[str] = None


class InputCancel(_Base):
    type: ChatServerMessageType = Field(default=ChatServerMessageType.INPUT_CANCEL)
    request_id: str = Field(alias="requestId")
    reason: Optional[str] = None


class RecordMessage(_Base):
    type: ChatServerMessageType = Field(default=ChatServerMessageType.RECORD)
    record: "FinalRecord"


# ---------------------------------------------------------------------------
# Client -> Server messages
# ---------------------------------------------------------------------------

class InputResponse(_Base):
    type: ChatClientMessageType = Field(default=ChatClientMessageType.INPUT_RESPONSE)
    request_id: str = Field(alias="requestId")
    field: str
    value: Any


# ---------------------------------------------------------------------------
# Final record & sub-models
# ---------------------------------------------------------------------------

class RecordField(_Base):
    value: Any
    source: Optional[FieldSource] = None
    collected_at: Optional[str] = Field(default=None, alias="collectedAt")
    status: FieldStatus


class RecordConflict(_Base):
    field: str
    call_value: Any = Field(alias="callValue")
    chat_value: Any = Field(alias="chatValue")
    resolution: str
    reason: str


class EngagementMetrics(_Base):
    calls: int
    call_seconds: int = Field(alias="callSeconds")
    chat_messages: int = Field(alias="chatMessages")


class FinalRecord(_Base):
    session_id: str = Field(alias="sessionId")
    revision: int
    outcome: Outcome
    outcome_reason: Optional[str] = Field(default=None, alias="outcomeReason")
    fields: dict[str, RecordField]
    conflicts: Optional[list[RecordConflict]] = None
    engagement: Optional[EngagementMetrics] = None
    cost_usd: Optional[float] = Field(default=None, alias="costUsd")
    ended_at: Optional[str] = Field(default=None, alias="endedAt")


# ---------------------------------------------------------------------------
# Internal session state (persisted to SQLite; not a wire message)
# ---------------------------------------------------------------------------

class SessionState(_Base):
    session_id: str
    revision: int = 0
    delivered_revision: int = 0
    outcome: Optional[Outcome] = None
    outcome_reason: Optional[str] = None
    fields: dict[str, RecordField] = {}
    failure_counts: dict[str, int] = {}
    conflicts: list[RecordConflict] = []
    open_request: Optional[InputRequest] = None
    email_prompted: bool = False
    confirm_rejections: int = 0
    injury_review_flag: bool = False
    vision_review_flag: bool = False
    engagement: EngagementMetrics = Field(
        default_factory=lambda: EngagementMetrics(
            calls=0, call_seconds=0, chat_messages=0
        )
    )
    cost_usd: float = 0.0
    latencies_ms: list[float] = []
    conversation_history: list[dict[str, Any]] = []
    created_at: str
    updated_at: str
    ended_at: Optional[str] = None
    last_activity: Optional[str] = None
    pending_ack_text: Optional[str] = None


# Resolve forward references.
RecordMessage.model_rebuild()
InputRequest.model_rebuild()

__all__ = [
    "Role",
    "CallServerMessageType",
    "ChatClientMessageType",
    "ChatServerMessageType",
    "InputKind",
    "Outcome",
    "FieldSource",
    "FieldStatus",
    "AudioMessage",
    "StopPlaybackMessage",
    "TranscriptMessage",
    "ChatMessage",
    "InputOption",
    "InputRequest",
    "InputError",
    "InputCancel",
    "RecordMessage",
    "InputResponse",
    "RecordField",
    "RecordConflict",
    "EngagementMetrics",
    "FinalRecord",
    "SessionState",
]
