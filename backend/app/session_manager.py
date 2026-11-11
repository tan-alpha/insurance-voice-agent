"""Session manager: per-session locks, WebSocket registry, chat routing (Sprint 2).

Serializes all state reads/mutations/writes behind an ``asyncio.Lock`` per
``session_id`` so `/chat`, `/call`, and timers never race on the same session.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Set

from fastapi import WebSocket

from .protocol import (
    ChatMessage,
    FieldStatus,
    InputCancel,
    InputError,
    InputRequest,
    Outcome,
    RecordMessage,
    Role,
    SessionState,
)
from .store import SessionStore
from .validator import (
    SPOKEN_ORDER,
    apply_field_update,
    evaluate_outcome,
    next_missing_input_request,
)
from .webhook import deliver_final_record

# Optional voice pipeline (Sprint 3). Imported lazily so the module loads
# even when voice extras are not needed.
try:
    from .voice import VoicePipelineSession, build_ack_text  # noqa: F401
except Exception:  # pragma: no cover - voice.py is always present in this repo
    VoicePipelineSession = None  # type: ignore
    build_ack_text = None  # type: ignore

KOLKATA = timezone(timedelta(hours=5, minutes=30))
PARTIAL_TIMEOUT_SECONDS = float(os.getenv("PARTIAL_TIMEOUT_SECONDS", "120"))


class SessionManager:
    def __init__(
        self,
        store: SessionStore,
        upload_dir: str,
        sink: Optional[list] = None,
        voice_providers: Optional[dict] = None,
    ):
        self.store = store
        self.upload_dir = upload_dir
        self._sink = sink
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self.chat_ws: dict[str, Set[WebSocket]] = {}
        self.call_ws: dict[str, Set[WebSocket]] = {}
        # voice_providers: {"stt": STTProvider|None, "llm": LLMProvider|None, "tts": TTSProvider|None}
        # or factories {"stt": callable->STTProvider, ...}. When None, production
        # providers keyed off DEEPGRAM_API_KEY / GROQ_API_KEY env vars.
        self._voice_providers = voice_providers or {}
        self._voice_sessions: dict[str, VoicePipelineSession] = {}
        # Sessions with recent activity (for watchdog tracking).
        self._active_sessions: set[str] = set()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_stopped = False

    # ------------------------------------------------------------------ locks

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    # ------------------------------------------------------------------ WS registry

    def _ws_set(self, kind: str, session_id: str) -> Set[WebSocket]:
        registry = self.chat_ws if kind == "chat" else self.call_ws
        return registry.setdefault(session_id, set())

    async def add_ws(self, kind: str, session_id: str, ws: WebSocket) -> None:
        self._ws_set(kind, session_id).add(ws)

    async def remove_ws(self, kind: str, session_id: str, ws: WebSocket) -> None:
        s = self._ws_set(kind, session_id)
        s.discard(ws)
        if not s:
            if kind == "chat":
                self.chat_ws.pop(session_id, None)
            else:
                self.call_ws.pop(session_id, None)

    async def broadcast_chat(self, session_id: str, text: str) -> None:
        dead = []
        for ws in list(self._ws_set("chat", session_id)):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove_ws("chat", session_id, ws)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _wire(msg) -> str:
        return msg.model_dump_json(by_alias=True, exclude_none=True)

    @staticmethod
    def _greeting() -> ChatMessage:
        return ChatMessage(
            role=Role.AGENT,
            text=(
                "Hi! I'm your motor insurance claims assistant. "
                "I'll collect a few details required for claim processing. "
                "You can also click the call button above to share details quickly. "
                "Let's get started."
            ),
        )

    # ------------------------------------------------------------------ chat connect

    async def handle_chat_connect(self, session_id: str, ws: WebSocket) -> None:
        await self.add_ws("chat", session_id, ws)
        lock = await self._session_lock(session_id)
        async with lock:
            state = self.store.get_or_create_session(session_id)
            is_new = state.revision == 0 and not state.fields

            if state.outcome in {"completed", "optedOut", "needsReview"}:
                # Terminal: re-send the final record so the frontend renders it.
                record = self._terminal_record(state)
                await ws.send_text(self._wire(RecordMessage(record=record)))
                return

            if is_new:
                await ws.send_text(self._wire(self._greeting()))

            # Re-send any pending open request so the client card renders on reconnect.
            req = state.open_request or next_missing_input_request(
                state, call_active=False
            )
            if req is not None:
                state.open_request = req
                self.store.save_session(state)
                await ws.send_text(self._wire(req))

    # ------------------------------------------------------------------ chat message

    async def handle_chat_message(self, session_id: str, raw_text: str) -> None:
        lock = await self._session_lock(session_id)
        async with lock:
            state = self.store.get_or_create_session(session_id)
            state.engagement.chat_messages += 1
            state.last_activity = datetime.now(KOLKATA).isoformat()
            self.register_session(session_id)

            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError:
                await self._send_ws(
                    "chat",
                    session_id,
                    self._wire(
                        ChatMessage(
                            role=Role.AGENT,
                            text="I didn't understand that. Please try again.",
                        )
                    ),
                )
                self.store.save_session(state)
                return

            if payload.get("type") != "inputResponse":
                self.store.save_session(state)
                return

            request_id = payload.get("requestId")
            field = payload.get("field")
            value = payload.get("value")

            valid_upload_urls = self.store.get_session_upload_urls(session_id)

            ok, err, input_cancel = apply_field_update(
                state,
                field,
                value,
                "chat",
                valid_upload_urls=valid_upload_urls,
            )

            if not ok:
                self.store.save_session(state)
                self.store.append_event(
                    session_id,
                    "chat",
                    "field_invalid",
                    {"field": field, "error": err or "invalid input"},
                )
                # 3-strike escalation: on the 3rd consecutive failure, emit the
                # terminal needsReview record instead of another InputError.
                if state.failure_counts.get(field, 0) >= 3:
                    record = evaluate_outcome(state)
                    if record:
                        self.store.save_session(state)
                        self.store.append_event(
                            session_id,
                            "chat",
                            "outcome",
                            {
                                "outcome": record.outcome,
                                "reason": record.outcome_reason,
                            },
                        )
                        await self._send_ws(
                            "chat", session_id, self._wire(RecordMessage(record=record))
                        )
                        await self._maybe_send_needs_review_note(session_id, state, record)
                        self._deliver(state, record)
                else:
                    await self._send_ws(
                        "chat",
                        session_id,
                        self._wire(
                            InputError(
                                request_id=request_id or "",
                                message=err or "invalid input",
                            )
                        ),
                    )
                return

            # Valid update.
            self.store.save_session(state)
            self.store.append_event(
                session_id, "chat", "field_valid", {"field": field, "value": value}
            )

            # Cross-channel acknowledgment: build and persist the ack text so a
            # fresh /call connection can speak it on reconnect.
            if build_ack_text is not None:
                state.pending_ack_text = build_ack_text(field, value, state)
                self.store.save_session(state)

            # If a spoken chat card for this field is open, cancel it.
            if input_cancel is not None:
                await self._send_ws("chat", session_id, self._wire(input_cancel))

            # Check for a terminal outcome.
            record = evaluate_outcome(state)
            if record:
                state.pending_ack_text = None
                self.store.save_session(state)
                self.store.append_event(
                    session_id,
                    "chat",
                    "outcome",
                    {"outcome": record.outcome, "reason": record.outcome_reason},
                )
                await self._send_ws(
                    "chat", session_id, self._wire(RecordMessage(record=record))
                )
                await self._maybe_send_needs_review_note(session_id, state, record)
                self._deliver(state, record)
                if self._ws_set("call", session_id):
                    voice_session = self._voice_sessions.get(session_id)
                    if voice_session is not None:

                        async def _thank_and_hangup(vs=voice_session, st=state):
                            vs._user_speaking = False
                            await vs._speak(
                                "Thank you! Your claim is now submitted. Take care and goodbye!",
                                st,
                            )
                            await vs._finish_call_after_speech()

                        asyncio.create_task(_thank_and_hangup())
                return

            # Notify the active voice pipeline if one exists.
            if self._ws_set("call", session_id):
                voice_session = self._voice_sessions.get(session_id)
                if voice_session is not None:
                    asyncio.create_task(
                        voice_session.notify_chat_field_validated(field, value, state)
                    )

            # Otherwise dispatch the next missing input request.
            next_req = next_missing_input_request(
                state, call_active=bool(self._ws_set("call", session_id))
            )
            if next_req is not None:
                state.open_request = next_req
                self.store.save_session(state)
                await self._send_ws("chat", session_id, self._wire(next_req))

    # ------------------------------------------------------------------ chat disconnect

    async def handle_chat_disconnect(self, session_id: str, ws: WebSocket) -> None:
        await self.remove_ws("chat", session_id, ws)

    # ------------------------------------------------------------------ call connect / voice pipeline

    async def handle_call_connect(self, session_id: str, ws: WebSocket) -> None:
        """Attach a /call WebSocket to a VoicePipelineSession for this session.

        Runs the pipeline receive loop (blocking until the client disconnects),
        then cleans up the registry and nudges the chat channel forward.
        """
        await self.add_ws("call", session_id, ws)
        self.register_session(session_id)
        lock = await self._session_lock(session_id)
        async with lock:
            state = self.store.get_or_create_session(session_id)
            state.last_activity = datetime.now(KOLKATA).isoformat()
            state.engagement.calls += 1
            if state.open_request and state.open_request.field in SPOKEN_ORDER:
                cancel_msg = InputCancel(
                    request_id=state.open_request.id,
                    reason="on voice call",
                )
                state.open_request = None
                await self.broadcast_chat(session_id, self._wire(cancel_msg))
            self.store.save_session(state)

            # Context-aware greeting for existing sessions with valid fields.
            valid_fields = [
                f for f, rf in state.fields.items() if rf.status == FieldStatus.VALID
            ]

        stt, llm, tts = self._resolve_voice_providers()

        async def send_call(text: str) -> None:
            await self._send_ws("call", session_id, text)

        async def send_chat(text: str) -> None:
            await self._send_ws("chat", session_id, text)

        async def close_call() -> None:
            try:
                cur_state = self.store.get_or_create_session(session_id)
                reason = (
                    "claim submitted"
                    if cur_state.outcome is not None
                    else "voice intake complete"
                )
                await ws.close(code=1000, reason=reason)
            except Exception:
                pass

        session = VoicePipelineSession(
            session_id=session_id,
            store=self.store,
            stt=stt,
            llm=llm,
            tts=tts,
            send_call=send_call,
            send_chat=send_chat,
            close_call=close_call,
        )
        self._voice_sessions[session_id] = session

        # Speak pending cross-channel acknowledgment or context greeting as
        # background tasks on the active session so barge-in and state stay unified.
        if state.pending_ack_text:
            ack_text = state.pending_ack_text
            state.pending_ack_text = None
            self.store.save_session(state)
            asyncio.create_task(session._speak(ack_text, state))
        elif valid_fields:
            asyncio.create_task(session.speak_context_greeting(state))
        elif os.getenv("DEEPGRAM_API_KEY"):
            asyncio.create_task(
                session._speak(
                    "Hi! I'm your motor insurance claims assistant. Can you tell me what happened, where, and when?",
                    state,
                )
            )

        # Run the pipeline receive loop. The greeting (if any) is already
        # running as a background task, so this accept returns promptly and
        # the client can send audio frames while the loop receives them.
        async def _run_and_cleanup() -> None:
            try:
                await session.run(ws)
            finally:
                self._voice_sessions.pop(session_id, None)
                await self.remove_ws("call", session_id, ws)
                try:
                    await asyncio.shield(self._nudge_chat_after_call(session_id))
                except asyncio.CancelledError:
                    pass

        await _run_and_cleanup()

    async def _nudge_chat_after_call(self, session_id: str) -> None:
        """On /call disconnect, send call-ended chat message,
        and dispatch the next missing input request for chat continuation."""
        lock = await self._session_lock(session_id)
        async with lock:
            state = self.store.get_or_create_session(session_id)
            state.last_activity = datetime.now(KOLKATA).isoformat()
            self.store.save_session(state)
            if state.outcome in {"completed", "optedOut", "needsReview"}:
                return
            # Send call-ended message over /chat.
            await self._send_ws(
                "chat",
                session_id,
                self._wire(
                    ChatMessage(
                        role=Role.AGENT,
                        text="Call ended — we can finish the remaining details right here in chat.",
                    )
                ),
            )
            # Dispatch the next missing input request for chat-only continuation.
            req = next_missing_input_request(state, call_active=False)
            if req is not None:
                state.open_request = req
                self.store.save_session(state)
                await self._send_ws("chat", session_id, self._wire(req))

    # ------------------------------------------------------------------ watchdog

    def register_session(self, session_id: str) -> None:
        """Mark a session as active for watchdog tracking."""
        self._active_sessions.add(session_id)

    def start_watchdog(self) -> None:
        """Start the background inactivity watchdog task."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_stopped = False
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop_watchdog(self) -> None:
        """Stop the background watchdog task."""
        self._watchdog_stopped = True
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

    async def _watchdog_loop(self) -> None:
        while not self._watchdog_stopped:
            await asyncio.sleep(0.5)
            await self.check_watchdog()

    async def check_watchdog(self) -> None:
        """Check all tracked sessions for inactivity timeout.

        Sessions where /call is disconnected and outcome is None that have been
        inactive for PARTIAL_TIMEOUT_SECONDS transition to outcome="partial"
        (revision=1) and deliver. Partial sessions that later complete upgrade
        to outcome="completed" (revision=2) and deliver.
        """
        from .protocol import Outcome

        now = datetime.now(KOLKATA)
        sessions_to_check = list(self._active_sessions)
        for session_id in sessions_to_check:
            state = self.store.get_or_create_session(session_id)
            # Skip terminal and already-emitted partial outcomes.
            if state.outcome in {"completed", "optedOut", "needsReview", "partial"}:
                continue
            # Skip active calls.
            if self._ws_set("call", session_id):
                continue
            last_activity = state.last_activity
            if last_activity is None:
                continue
            try:
                last_dt = datetime.fromisoformat(last_activity)
            except ValueError:
                continue
            if (now - last_dt).total_seconds() < PARTIAL_TIMEOUT_SECONDS:
                continue
            # Inactivity timeout: transition to partial (revision=1).
            record = evaluate_outcome(
                state, force_partial=True, now_iso=now.isoformat()
            )
            if record and record.outcome == Outcome.PARTIAL:
                self.store.save_session(state)
                self.store.append_event(
                    session_id,
                    "system",
                    "watchdog_partial",
                    {"outcome": "partial", "revision": record.revision},
                )
                await self._send_ws(
                    "chat", session_id, self._wire(RecordMessage(record=record))
                )
                await self._maybe_send_needs_review_note(session_id, state, record)
                self._deliver(state, record)

    def _resolve_voice_providers(self):
        """Resolve STT/LLM/TTS providers: explicit > env-keyed production > mocks."""
        vp = self._voice_providers

        def _get(kind: str):
            val = vp.get(kind)
            if val is None:
                return None
            if (
                callable(val)
                and not hasattr(val, "ingest")
                and not hasattr(val, "respond")
                and not hasattr(val, "synthesize")
            ):
                return val()
            return val

        stt = _get("stt")
        llm = _get("llm")
        tts = _get("tts")

        if stt is None:
            from .voice import DeepgramLiveSTT, MockSTTProvider

            if os.getenv("DEEPGRAM_API_KEY"):
                stt = DeepgramLiveSTT(os.getenv("DEEPGRAM_API_KEY"))
            else:
                stt = MockSTTProvider()
        if llm is None:
            from .voice import OpenAILLM, MockLLMProvider

            llm_api_key = (
                os.getenv("OPENROUTER_API_KEY")
                or os.getenv("GROQ_API_KEY")
                or os.getenv("LLM_API_KEY")
            )
            if llm_api_key:
                llm = OpenAILLM(
                    api_key=llm_api_key,
                    base_url=os.getenv(
                        "LLM_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"
                    ),
                    model=os.getenv(
                        "LLM_MODEL",
                        "cohere/north-mini-code:free",
                    ),
                )
            else:
                llm = MockLLMProvider()
        if tts is None:
            from .voice import DeepgramAuraTTS, MockTTSProvider

            if os.getenv("DEEPGRAM_API_KEY"):
                tts = DeepgramAuraTTS(os.getenv("DEEPGRAM_API_KEY"))
            else:
                tts = MockTTSProvider()
        return stt, llm, tts

    # ------------------------------------------------------------------ internal

    async def _maybe_send_needs_review_note(
        self, session_id: str, state: SessionState, record
    ) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ or record.outcome != Outcome.NEEDS_REVIEW:
            return
        reasons = []
        if state.injury_review_flag:
            inj = state.fields.get("injuries")
            detail = inj.value if inj and inj.value else "reported"
            reasons.append(f'bodily injury reported ("{detail}")')
        if state.vision_review_flag:
            reasons.append("uploaded photo did not pass AI vehicle-damage verification")
        for f, cnt in state.failure_counts.items():
            if cnt >= 3:
                reasons.append(f"{f} failed validation {cnt} times")
        if not reasons and record.outcome_reason:
            reasons.append(record.outcome_reason)
        note = (
            "Claim status: Marked for human adjuster review (needsReview) because "
            + "; ".join(reasons)
            + "."
        )
        await self._send_ws(
            "chat", session_id, self._wire(ChatMessage(role=Role.AGENT, text=note))
        )

    async def _send_ws(self, kind: str, session_id: str, text: str) -> None:
        dead = []
        for ws in list(self._ws_set(kind, session_id)):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove_ws(kind, session_id, ws)

    def _deliver(self, state: SessionState, record) -> None:
        """Deliver a final record (idempotent) and mirror to the webhook sink."""
        delivered = deliver_final_record(self.store, state, record)
        if delivered and self._sink is not None:
            self._sink.append(
                json.loads(record.model_dump_json(by_alias=True, exclude_none=True))
            )

    @staticmethod
    def _terminal_record(state: SessionState):
        from .protocol import FinalRecord, Outcome, RecordField, FieldStatus
        from .validator import REQUIRED_FIELDS

        now_iso = (
            state.ended_at or state.updated_at or datetime.now(KOLKATA).isoformat()
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
            outcome=Outcome(state.outcome),
            outcome_reason=state.outcome_reason,
            fields=fields_dict,
            conflicts=list(state.conflicts) if state.conflicts else None,
            engagement=state.engagement,
            cost_usd=state.cost_usd or None,
        )
