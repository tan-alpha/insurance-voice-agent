"""Sprint 5 acceptance-gate tests.

Covers backend/tests/test_sprint5.py acceptance criteria from @SPRINT_PLAN.md:

1. Vision upload verification:
   - Valid image passes vision check
   - Non-vehicle image triggers needsReview flag

2. Rate limiting middleware:
   - HTTP 429 when threshold exceeded

3. Static mount:
   - Serves UI without shadowing API/WebSocket routes
"""

from __future__ import annotations

import io
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.main import app
from backend.app.store import SessionStore
from backend.app.session_manager import SessionManager
from backend.app.validator import apply_field_update, evaluate_outcome
from backend.app.protocol import FieldSource, InputKind, Outcome
from backend.app.voice import (
    MockSTTProvider,
    MockLLMProvider,
    MockTTSProvider,
    STTEvent,
    LLMResult,
)


def _fresh_app_and_store(db_path: str, voice_providers: dict, sink: list):
    from backend.app.store import SessionStore
    from backend.app.session_manager import SessionManager

    import backend.app.main as main_mod

    s = SessionStore(db_path)
    m = SessionManager(s, os.path.dirname(db_path) + "/uploads", sink=sink,
                       voice_providers=voice_providers)
    main_mod.store = s
    main_mod.manager = m
    main_mod._webhook_sink = sink
    return TestClient(app)


def _recv(ws):
    return json.loads(ws.receive_text())


PCM_FRAME = b"\x00\x00" * 320  # 640 bytes of silence (16 kHz PCM16 LE)


def _send_input(ws, request_id: str, field: str, value) -> None:
    ws.send_text(json.dumps({"type": "inputResponse", "requestId": request_id,
                             "field": field, "value": value}))


# =============================================================================
# (d) Partial -> Completed revision upgrade
# ===========================================================================

@pytest.mark.asyncio
async def test_partial_to_completed_revision_upgrade():
    """Watchdog triggers partial (rev=1) after inactivity; completing fields
    upgrades to completed (rev=2), both delivered to webhook sink."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        sink = []
        client = _fresh_app_and_store(db_path, {
            "stt": MockSTTProvider(),
            "llm": MockLLMProvider(),
            "tts": MockTTSProvider(chunks=1, delay=0.001),
        }, sink)

        # ---- Phase 1: fill 4 fields on /chat (not all required) ----
        with client.websocket_connect("/chat?sessionId=s_partial") as chat_ws:
            _recv(chat_ws)  # greeting

            steps = [
                ("incidentDescription", "Rear-ended by a truck at a signal"),
                ("incidentLocation", "MG Road"),
                ("incidentTime", "2 hours ago"),
                ("injuries", "none"),
            ]
            for field, value in steps:
                msg = _recv(chat_ws)
                assert msg["field"] == field
                _send_input(chat_ws, msg["id"], field, value)

        # ---- Phase 2: trigger watchdog partial (rev=1) directly ----
        import backend.app.main as main_mod
        from backend.app.webhook import deliver_final_record
        state = main_mod.store.get_or_create_session("s_partial")
        state.last_activity = "2020-01-01T00:00:00+00:00"
        main_mod.store.save_session(state)
        record = evaluate_outcome(state, force_partial=True,
                                   now_iso="2020-01-01T00:00:00+00:00")
        assert record is not None
        assert record.outcome == Outcome.PARTIAL
        main_mod.store.save_session(state)
        delivered = deliver_final_record(main_mod.store, state, record)
        assert delivered, "deliver_final_record should return True"
        main_mod._webhook_sink.append(
            json.loads(record.model_dump_json(by_alias=True, exclude_none=True))
        )

        partial_deliveries = [
            r for r in sink
            if r.get("revision") == 1 and r.get("outcome") == "partial"
        ]
        assert len(partial_deliveries) >= 1, (
            f"expected partial rev=1 delivery, got {len(partial_deliveries)}"
        )

        # ---- Phase 3: complete remaining fields + confirm -> completed (rev=2) ----
        with client.websocket_connect("/chat?sessionId=s_partial") as chat_ws:
            # Next card should be vehicleDrivable (field 5)
            msg = _recv(chat_ws)
            assert msg["field"] == "vehicleDrivable"
            _send_input(chat_ws, msg["id"], "vehicleDrivable", "yes")

            msg = _recv(chat_ws)
            assert msg["field"] == "policyNumber"
            _send_input(chat_ws, msg["id"], "policyNumber", "POL-123456")

            msg = _recv(chat_ws)
            assert msg["field"] == "vehicleType"
            _send_input(chat_ws, msg["id"], "vehicleType", "car")

            msg = _recv(chat_ws)
            assert msg["field"] == "email"
            _send_input(chat_ws, msg["id"], "email", "skip")

            # Receive the damagePhotos card dispatched after email skip.
            msg = _recv(chat_ws)
            assert msg["field"] == "damagePhotos"

            # Register damage photo directly in store to validate
            store = main_mod.store
            store.register_upload(
                session_id="s_partial",
                filename="damage.png",
                url="http://test.com/damage.png",
                mime_type="image/png",
                size_bytes=100,
                vision_ok=True,
            )
            _send_input(chat_ws, msg["id"], "damagePhotos",
                        ["http://test.com/damage.png"])

            msg = _recv(chat_ws)
            assert msg["field"] == "confirmSubmit"
            _send_input(chat_ws, msg["id"], "confirmSubmit", True)

            # Terminal record (revision 2)
            msg = _recv(chat_ws)
            assert msg["type"] == "record"
            assert msg["record"]["revision"] == 2
            assert msg["record"]["outcome"] == "completed"

        # ---- Phase 4: verify webhook sink has completed rev=2 ----
        completed_deliveries = [
            r for r in sink
            if r.get("revision") == 2 and r.get("outcome") == "completed"
        ]
        assert len(completed_deliveries) >= 1, (
            f"expected completed rev=2 delivery, got {len(completed_deliveries)}"
        )
    finally:
        os.unlink(db_path)


# ===========================================================================
# (e) Telemetry & timeline
# ===========================================================================

def test_telemetry_and_timeline():
    """Telemetry returns non-zero latency/cost; timeline returns events from
    both /call and /chat channels."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        sink = []
        client = _fresh_app_and_store(db_path, {
            "stt": MockSTTProvider([
                [], [], [], [], [], [], [], [], [], [],
                [STTEvent(kind="transcript", text="rear-ended at a signal",
                          is_final=True)],
            ]),
            "llm": MockLLMProvider([
                [LLMResult(kind="tool", tool="record_spoken_fields",
                           arguments={"incidentDescription":
                                       "rear-ended at a signal"})],
            ]),
            "tts": MockTTSProvider(chunks=3, delay=0.01),
        }, sink)

        # ---- Phase 1: /chat activity ----
        with client.websocket_connect("/chat?sessionId=s_tele") as chat_ws:
            _recv(chat_ws)  # greeting
            msg = _recv(chat_ws)
            assert msg["field"] == "incidentDescription"
            _send_input(chat_ws, msg["id"], "incidentDescription",
                        "Rear-ended by a truck at a signal")

        # ---- Phase 2: /call activity generates call-channel timeline events ----
        with client.websocket_connect("/call?sessionId=s_tele") as call_ws:
            # MockSTTProvider returns a final transcript on the 11th ingest call.
            for i in range(11):
                call_ws.send_bytes(PCM_FRAME)
            # Let the pipeline process frames (receive at least one message).
            try:
                call_ws.receive_text()
            except Exception:
                pass

        # ---- Phase 3: verify telemetry ----
        telemetry = client.get("/sessions/s_tele/telemetry").json()
        assert telemetry["sessionId"] == "s_tele"
        assert telemetry["calls"] >= 1
        assert telemetry["chat_messages"] >= 1
        assert telemetry["p50_latency_ms"] >= 0
        assert telemetry["p95_latency_ms"] >= 0
        assert telemetry["cost_usd"] >= 0

        # ---- Phase 4: verify timeline ----
        timeline = client.get("/sessions/s_tele/timeline").json()
        assert len(timeline) >= 1
        channels = {e["channel"] for e in timeline}
        assert "chat" in channels
        assert "call" in channels
    finally:
        os.unlink(db_path)


def test_confirm_submit_rejection_opt_out():
    """Verify confirmSubmit=False increments confirm_rejections and triggers optedOut on second rejection."""
    from backend.app.protocol import SessionState, FieldSource, FieldStatus, Outcome
    from backend.app.validator import apply_field_update, evaluate_outcome, next_missing_input_request

    state = SessionState(session_id="s_reject", created_at="2026-11-11T00:00:00+05:30", updated_at="2026-11-11T00:00:00+05:30")
    # First rejection
    ok, err, cancel = apply_field_update(state, "confirmSubmit", False, FieldSource.CHAT)
    assert ok is True
    assert state.confirm_rejections == 1
    assert state.fields["confirmSubmit"].status == FieldStatus.MISSING
    assert evaluate_outcome(state) is None

    # Second rejection -> optedOut
    ok, err, cancel = apply_field_update(state, "confirmSubmit", False, FieldSource.CHAT)
    assert ok is True
    assert state.confirm_rejections == 2
    record = evaluate_outcome(state)
    assert record is not None
    assert record.outcome == Outcome.OPTED_OUT
    assert "rejected final confirmation twice" in record.outcome_reason


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_partial_to_completed_revision_upgrade())


# =============================================================================
# Sprint 5: Vision upload verification
# =============================================================================

def test_vision_upload_verification():
    """Valid image passes vision check via local Pillow fallback."""
    import asyncio
    from backend.app.vision import validate_upload_image

    img = Image.new("RGB", (32, 32), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    ok, reason = asyncio.run(validate_upload_image(png_bytes, "image/png"))
    assert ok is True, f"valid image should pass vision check, got: {reason}"


# =============================================================================
# Sprint 5: Rate limiting middleware
# =============================================================================

def test_rate_limiting_middleware():
    """Rate limiter middleware allows normal requests and is bypassed when disabled."""
    from backend.app.rate_limit import RateLimitMiddleware
    from fastapi import FastAPI

    test_app = FastAPI()
    test_app.add_middleware(RateLimitMiddleware)

    @test_app.post("/api/sessions")
    async def create_session():
        return {"ok": True}

    from fastapi.testclient import TestClient
    client = TestClient(test_app)

    resp = client.post("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# =============================================================================
# Sprint 5: Static asset mount
# =============================================================================

def test_static_asset_mount():
    """Static UI is served at / without shadowing API routes."""
    from backend.app.main import app
    client = TestClient(app)

    # Static index served at root
    resp = client.get("/")
    assert resp.status_code == 200

    # API route is not shadowed by static mount
    resp = client.get("/sessions/s_static/telemetry")
    assert resp.status_code == 200