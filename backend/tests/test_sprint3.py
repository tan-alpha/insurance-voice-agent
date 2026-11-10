"""Sprint 3 acceptance-gate tests (offline, mock providers).

Covers backend/tests/test_sprint3.py acceptance criteria from @SPRINT_PLAN.md:
  a) Full audio round-trip: 640-byte PCM frames -> TranscriptMessage + AudioMessage.
  b) Tool execution: record_spoken_fields sets spoken fields with source="call"
     and dispatches the first chat card (policyNumber) on /chat.
  c) Barge-in cancellation: user speech during agent TTS -> immediate stopPlayback.
  d) Auto-hangup: completed outcome while /call connected -> close_call invoked.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from fastapi.testclient import TestClient

from backend.app.voice import (
    LLMResult,
    MockLLMProvider,
    MockSTTProvider,
    MockTTSProvider,
    STTEvent,
    VoicePipelineSession,
)
from backend.app.protocol import (
    FieldSource,
    FieldStatus,
    InputKind,
    RecordField,
    Role,
)
from backend.app.store import SessionStore
from backend.app.session_manager import SessionManager
from backend.app.main import app

PCM_FRAME = b"\x00\x00" * 320  # 640 bytes of silence (16 kHz PCM16 LE)


import pytest


def _fresh_app_and_store(db_path: str, voice_providers: dict, sink: list):
    from backend.app.store import SessionStore
    from backend.app.session_manager import SessionManager

    import backend.app.main as main_mod

    s = SessionStore(db_path)
    m = SessionManager(s, os.path.dirname(db_path) + "/uploads", sink=sink,
                       voice_providers=voice_providers)
    main_mod.store = s
    main_mod.manager = m
    main_mod._webhook_sink.clear()
    return TestClient(app)


def _recv(ws):
    return json.loads(ws.receive_text())


# ---------------------------------------------------------------------------
# (a) Full audio round-trip
# ---------------------------------------------------------------------------

def test_audio_round_trip_transcript_and_audio():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        sink = []
        client = _fresh_app_and_store(db_path, {
            "stt": MockSTTProvider([
                [], [], [], [], [], [], [], [], [],
                [STTEvent(kind="transcript", text="I was rear-ended at a signal", is_final=True)],
            ]),
            "llm": MockLLMProvider([
                [LLMResult(kind="text", text="I'm sorry to hear that. Where did this happen?")],
            ]),
            "tts": MockTTSProvider(chunks=2, delay=0.005),
        }, sink)

        with client.websocket_connect("/call?sessionId=s_audio") as ws:
            # Send 10 frames; the 10th triggers the transcript.
            for i in range(10):
                ws.send_bytes(PCM_FRAME)
                if i == 9:
                    # The transcript + agent TTS audio should now be available.
                    user_transcript = None
                    audio_msg = None
                    for _ in range(50):
                        msg = _recv(ws)
                        if msg.get("type") == "transcript" and msg.get("role") == "user":
                            user_transcript = msg
                        if msg.get("type") == "audio":
                            audio_msg = msg
                        if user_transcript and audio_msg:
                            break

            assert user_transcript is not None, "expected TranscriptMessage(role=user)"
            assert user_transcript["text"] == "I was rear-ended at a signal"
            assert user_transcript["final"] is True
            assert audio_msg is not None, "expected AudioMessage from agent TTS"
            assert audio_msg["sampleRate"] == 24000
            assert "data" in audio_msg
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# (b) Tool execution: spoken fields + first chat card on /chat
# ---------------------------------------------------------------------------

def test_tool_execution_spoken_fields_and_chat_card():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        sink = []
        client = _fresh_app_and_store(db_path, {
            "stt": MockSTTProvider([
                # 5 transcripts, each preceded by 10 silence frames (55 total)
                [], [], [], [], [], [], [], [], [],
                [STTEvent(kind="transcript", text="rear-ended at a signal", is_final=True)],
                [], [], [], [], [], [], [], [], [],
                [STTEvent(kind="transcript", text="MG Road", is_final=True)],
                [], [], [], [], [], [], [], [], [],
                [STTEvent(kind="transcript", text="2 hours ago", is_final=True)],
                [], [], [], [], [], [], [], [], [],
                [STTEvent(kind="transcript", text="nobody hurt", is_final=True)],
                [], [], [], [], [], [], [], [], [],
                [STTEvent(kind="transcript", text="yes", is_final=True)],
            ]),
            "llm": MockLLMProvider([
                [LLMResult(kind="tool", tool="record_spoken_fields",
                           arguments={"incidentDescription": "rear-ended at a signal"})],
                [LLMResult(kind="tool", tool="record_spoken_fields",
                           arguments={"incidentLocation": "MG Road"})],
                [LLMResult(kind="tool", tool="record_spoken_fields",
                           arguments={"incidentTime": "2 hours ago"})],
                [LLMResult(kind="tool", tool="record_spoken_fields",
                           arguments={"injuries": "none"})],
                [LLMResult(kind="tool", tool="record_spoken_fields",
                           arguments={"vehicleDrivable": True})],
            ]),
            "tts": MockTTSProvider(chunks=1, delay=0.001),
        }, sink)

        # ---- Connect /call and collect all 5 spoken fields via voice ----
        with client.websocket_connect("/call?sessionId=s_tool") as call_ws:
            for _ in range(55):
                call_ws.send_bytes(PCM_FRAME)

        # ---- Verify store has all 5 spoken fields with source="call" ----
        store = SessionStore(db_path)
        state = store.get_or_create_session("s_tool")
        for f in ["incidentDescription", "incidentLocation", "incidentTime",
                  "injuries", "vehicleDrivable"]:
            rf = state.fields.get(f)
            assert rf is not None, f"missing field {f}"
            assert rf.status == FieldStatus.VALID, f"field {f} not valid"
            assert rf.source == FieldSource.CALL, (
                f"field {f} source should be call, got {rf.source}"
            )

        # ---- Reconnect /chat: first card should be policyNumber (spoken fields done) ----
        with client.websocket_connect("/chat?sessionId=s_tool") as chat_ws:
            msg = _recv(chat_ws)
            assert msg["type"] == "inputRequest"
            assert msg["field"] == "policyNumber"
            assert msg["kind"] == InputKind.TEXT
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# (c) Barge-in cancellation (unit-level on VoicePipelineSession)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_barge_in_cancellation():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SessionStore(db_path)
        sent = []

        async def send_call(text: str):
            sent.append(text)

        session = VoicePipelineSession(
            session_id="s_barge",
            store=store,
            stt=MockSTTProvider(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(chunks=10, delay=0.02),
            send_call=send_call,
            send_chat=lambda text: None,
            close_call=lambda: None,
        )

        state = store.get_or_create_session("s_barge")

        # Agent starts speaking (TTS streaming).
        speak_task = asyncio.create_task(session._speak("Hello, how can I help?", state))
        # Give TTS a moment to start streaming.
        await asyncio.sleep(0.05)
        assert session._agent_speaking is True
        assert len(sent) > 0, "expected agent audio frames while speaking"

        # User speech arrives while agent is speaking -> barge-in.
        await session._handle_barge_in()
        # Let cancellation propagate through the event loop.
        await asyncio.sleep(0.1)

        # stopPlayback must have been sent immediately.
        stop_msgs = [s for s in sent if "stopPlayback" in s]
        assert len(stop_msgs) >= 1, "expected stopPlayback on barge-in"

        # Agent speaking flag must be reset.
        assert session._agent_speaking is False

        # The speech task should be cancelled/done.
        assert speak_task.done() or speak_task.cancelled()

        # No further stale audio frames after stopPlayback.
        stop_index = next(i for i, s in enumerate(sent) if "stopPlayback" in s)
        audio_after_stop = [s for s in sent[stop_index + 1:] if '"type":"audio"' in s or '"type": "audio"' in s]
        assert len(audio_after_stop) == 0, "expected no stale audio after stopPlayback"
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# (d) Auto-hangup on completed outcome
# ---------------------------------------------------------------------------

def test_auto_hangup_on_completed():
    """When the session reaches ``completed`` while /call is connected,
    the voice pipeline invokes ``close_call`` (auto-hangup)."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SessionStore(db_path)
        close_called = []

        async def async_lambda_send(text: str):
            pass

        async def close_call():
            close_called.append(True)

        session = VoicePipelineSession(
            session_id="s_hangup",
            store=store,
            stt=MockSTTProvider(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(),
            send_call=async_lambda_send,
            send_chat=async_lambda_send,
            close_call=close_call,
        )

        state = store.get_or_create_session("s_hangup")
        now = "2026-11-10T19:15:00+05:30"
        apply = __import__("backend.app.validator", fromlist=["apply_field_update"]).apply_field_update
        apply(state, "incidentDescription", "Rear-ended at signal", FieldSource.CALL, now)
        apply(state, "incidentLocation", "MG Road", FieldSource.CALL, now)
        apply(state, "incidentTime", "2 hours ago", FieldSource.CALL, now)
        apply(state, "injuries", "none", FieldSource.CALL, now)
        apply(state, "vehicleDrivable", True, FieldSource.CALL, now)
        apply(state, "policyNumber", "POL-123456", FieldSource.CHAT, now)
        apply(state, "vehicleType", "car", FieldSource.CHAT, now)
        apply(state, "damagePhotos", ["http://x/1.png"], FieldSource.CHAT, now,
              valid_upload_urls={"http://x/1.png"})
        apply(state, "email", "skip", FieldSource.CHAT, now)
        apply(state, "confirmSubmit", True, FieldSource.CHAT, now)
        store.save_session(state)

        from backend.app.validator import evaluate_outcome, Outcome
        record = evaluate_outcome(state)
        assert record is not None
        assert record.outcome == Outcome.COMPLETED

        # Run the pipeline outcome check: should invoke close_call (auto-hangup).
        asyncio.run(session._check_outcome(state))

        assert len(close_called) == 1, "expected close_call (auto-hangup) to be invoked"
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# VoicePipelineSession direct unit test: barge-in resets _agent_speaking
# ---------------------------------------------------------------------------

def test_barge_in_resets_agent_speaking_flag():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SessionStore(db_path)
        sent = []

        async def send_call(text: str):
            sent.append(text)

        session = VoicePipelineSession(
            session_id="s_flag",
            store=store,
            stt=MockSTTProvider(),
            llm=MockLLMProvider(),
            tts=MockTTSProvider(chunks=5, delay=0.02),
            send_call=send_call,
            send_chat=lambda text: None,
            close_call=lambda: None,
        )

        state = store.get_or_create_session("s_flag")

        async def run_speak():
            session._agent_speaking = True
            await session._speak("Hello world", state)

        asyncio.run(run_speak())
        assert session._agent_speaking is False, (
            "agent speaking flag should reset after TTS ends"
        )
    finally:
        os.unlink(db_path)
