"""Real-time voice pipeline for the /call WebSocket (Sprint 3).

Architecture: STT -> LLM -> TTS with pluggable providers.

* ``STTProvider`` ingests raw PCM16 LE mono frames (16 kHz, 20 ms / 640 bytes)
  and emits transcript / speech events.
* ``LLMProvider`` takes a user transcript + session-state summary and returns
  tool calls and/or plain agent text.
* ``TTSProvider`` synthesizes agent text into raw PCM16 LE mono chunks
  (24 kHz) that are base64-encoded into ``AudioMessage`` packets.

Production providers (Deepgram Live STT, OpenAI-compatible LLM, Deepgram
Aura TTS) are used when ``DEEPGRAM_API_KEY`` / ``OPENROUTER_API_KEY`` are
present. Deterministic mock providers are used otherwise so the test suite
runs 100% offline without live API credentials.

Barge-in: when a user-speech event arrives while the agent is actively
streaming TTS, the running speech task is cancelled immediately, a
``StopPlaybackMessage`` is sent over /call, and a ``barge_in`` event is
logged to SQLite.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator, Callable, List, Optional

from .protocol import (
    AudioMessage,
    FieldSource,
    FieldStatus,
    InputCancel,
    InputKind,
    InputRequest,
    RecordMessage,
    Role,
    SessionState,
    StopPlaybackMessage,
    TranscriptMessage,
    Outcome,
)
from .store import SessionStore
from .validator import apply_field_update, evaluate_outcome, next_missing_input_request

# ---------------------------------------------------------------------------
# Provider event / result data structures
# ---------------------------------------------------------------------------


@dataclass
class STTEvent:
    kind: str  # "transcript" | "speech_started" | "speech_stopped"
    text: str = ""
    is_final: bool = False


@dataclass
class LLMResult:
    kind: str  # "tool" | "text"
    tool: Optional[str] = None
    arguments: dict = field(default_factory=dict)
    text: Optional[str] = None


# ---------------------------------------------------------------------------
# Provider interfaces
# ---------------------------------------------------------------------------


class STTProvider(ABC):
    @abstractmethod
    async def ingest(self, pcm_frame: bytes) -> List[STTEvent]:
        """Feed one 640-byte PCM16 LE frame; return any STT events produced."""


class LLMProvider(ABC):
    @abstractmethod
    async def respond(
        self, transcript: str, state_summary: str
    ) -> List[LLMResult]:
        """Given a final user transcript, return tool results and/or agent text."""


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield raw PCM16 LE mono chunks at 24 kHz."""


# ---------------------------------------------------------------------------
# Production: Deepgram Live STT
# ---------------------------------------------------------------------------


class DeepgramLiveSTT(STTProvider):
    """Streams PCM frames to Deepgram Nova-3 over WebSocket."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._ws: Optional[Any] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._event_queue: asyncio.Queue[STTEvent] = asyncio.Queue()

    async def _connect(self) -> None:
        import websockets  # type: ignore

        url = (
            "wss://api.deepgram.com/v1/listen?"
            "model=nova-3&encoding=linear16&sample_rate=16000&channels=1"
            "&interim_results=true&utterance_end_ms=1000&vad_events=true"
            "&endpointing=300"
        )
        self._ws = await websockets.connect(
            url, additional_headers={"Authorization": f"Token {self.api_key}"}
        )
        self._connect_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "SpeechStarted":
                self._event_queue.put_nowait(STTEvent(kind="speech_started"))
            elif data.get("is_final"):
                transcript = data.get("channel", {}).get("alternatives", [{}])[0].get(
                    "transcript", ""
                )
                if transcript:
                    self._event_queue.put_nowait(
                        STTEvent(kind="transcript", text=transcript, is_final=True)
                    )

    async def ingest(self, pcm_frame: bytes) -> List[STTEvent]:
        if self._ws is None:
            await self._connect()
        assert self._ws is not None
        await self._ws.send(pcm_frame)
        events: List[STTEvent] = []
        while not self._event_queue.empty():
            events.append(self._event_queue.get_nowait())
        return events

    async def close(self) -> None:
        if self._connect_task:
            self._connect_task.cancel()
        if self._ws:
            await self._ws.close()


# ---------------------------------------------------------------------------
# Production: Groq tool-calling LLM
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Production: OpenAI-compatible LLM (OpenRouter / Groq / etc.)
# ---------------------------------------------------------------------------


class OpenAILLM(LLMProvider):
    """OpenAI-compatible chat completions (OpenRouter, Groq, etc.)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1/chat/completions",
        model: str = "nvidia/nemotron-3.5-lightning:free",
    ):
        self.api_key = api_key
        base = base_url.rstrip("/")
        if not base.endswith("/chat/completions"):
            base = f"{base}/chat/completions"
        self.base_url = base
        self.model = model

    def _tools(self) -> List[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "record_spoken_fields",
                    "description": "Record fields collected on the voice call.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "incidentDescription": {"type": "string"},
                            "incidentLocation": {"type": "string"},
                            "incidentTime": {"type": "string"},
                            "injuries": {"type": "string"},
                            "vehicleDrivable": {"type": "boolean"},
                            "email": {"type": "string"},
                            "policyNumber": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "request_chat_card",
                    "description": "Request a chat input card for a field.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "enum": [
                                    "policyNumber",
                                    "vehicleType",
                                    "damagePhotos",
                                    "email",
                                    "confirmSubmit",
                                ],
                            }
                        },
                        "required": ["field"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "opt_out_session",
                    "description": "User opted out of the claim session.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string"},
                        },
                        "required": ["reason"],
                    },
                },
            },
        ]

    def _system_prompt(self, state_summary: str) -> str:
        return (
            "You are a motor insurance claims voice assistant. "
            "Collect the claim details through conversation. "
            f"Current session state: {state_summary}. "
            "Use record_spoken_fields to persist anything the user says. "
            "Use request_chat_card when a field is better collected in chat "
            "(policy number, vehicle type, damage photos, email, confirmation). "
            "Use opt_out_session if the user explicitly cancels. "
            "Be concise and natural."
        )

    async def respond(self, transcript: str, state_summary: str) -> List[LLMResult]:
        import httpx  # type: ignore

        messages = [
            {"role": "system", "content": self._system_prompt(state_summary)},
            {"role": "user", "content": transcript},
        ]
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "tools": self._tools(),
            "tool_choice": "auto",
            "temperature": 0.3,
        }
        # Nemotron models need chat_template_kwargs to suppress reasoning.
        if any(k in self.model.lower() for k in ("nemotron", "llama-4-scout")):
            payload["chat_template_kwargs"] = {
                "enable_thinking": False,
                "force_nonempty_content": True,
            }
        results: List[LLMResult] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]["message"]
        if choice.get("tool_calls"):
            for tc in choice["tool_calls"]:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                results.append(
                    LLMResult(kind="tool", tool=fn["name"], arguments=args)
                )
        if choice.get("content"):
            results.append(LLMResult(kind="text", text=choice["content"]))
        return results


# ---------------------------------------------------------------------------
# Production: Deepgram Aura TTS
# ---------------------------------------------------------------------------


class DeepgramAuraTTS(TTSProvider):
    """Generates 24 kHz PCM16 LE audio from Deepgram Aura."""

    def __init__(self, api_key: str, voice: str = "aura-2-thalia-en"):
        self.api_key = api_key
        self.voice = voice
        self._base_url = (
            "https://api.deepgram.com/v1/speak"
            f"?model={voice}&encoding=linear16&sample_rate=24000&container=none"
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        import httpx  # type: ignore

        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                self._base_url,
                headers={"Authorization": f"Token {self.api_key}"},
                content=text,
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk


# ---------------------------------------------------------------------------
# Deterministic mock providers (offline testing)
# ---------------------------------------------------------------------------


class MockSTTProvider(STTProvider):
    """Returns a scripted list of STTEvents per ``ingest`` call."""

    def __init__(self, scripts: Optional[List[List[STTEvent]]] = None):
        self._scripts = scripts or []
        self._call_count = 0

    async def ingest(self, pcm_frame: bytes) -> List[STTEvent]:
        self._call_count += 1
        idx = self._call_count - 1
        if idx < len(self._scripts):
            return list(self._scripts[idx])
        return []


class MockLLMProvider(LLMProvider):
    """Returns scripted LLMResults per ``respond`` call."""

    def __init__(self, scripts: Optional[List[List[LLMResult]]] = None):
        self._scripts = scripts or []
        self._call_count = 0

    async def respond(self, transcript: str, state_summary: str) -> List[LLMResult]:
        self._call_count += 1
        idx = self._call_count - 1
        if idx < len(self._scripts):
            return list(self._scripts[idx])
        return []


class MockTTSProvider(TTSProvider):
    """Yields dummy 24 kHz PCM16 LE base64-encodable chunks with a small delay."""

    def __init__(self, chunks: int = 3, delay: float = 0.01):
        self._chunks = chunks
        self._delay = delay

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        sample = b"\x00\x00" * 160  # 160 samples = ~6.67 ms at 24 kHz
        for _ in range(self._chunks):
            await asyncio.sleep(self._delay)
            yield sample


# ---------------------------------------------------------------------------
# Cross-channel acknowledgment helper
# ---------------------------------------------------------------------------

def build_ack_text(field: str, value: Any, state: SessionState) -> str:
    """Build a concise acknowledgment for a chat-validated field."""
    field_label = field.replace("_", " ").title()
    next_req = next_missing_input_request(state, call_active=True)
    next_field = next_req.field if next_req else None
    next_label = next_field.replace("_", " ").title() if next_field else None

    if field == "policyNumber":
        if next_label:
            return f"Got your policy number {value}. I've sent the {next_label} card to your chat."
        return f"Got your policy number {value}."
    if field == "email":
        return f"Got your email {value}."
    if field == "vehicleType":
        return f"Got it, a {value}."
    if field == "damagePhotos":
        return f"Got {len(value) if isinstance(value, list) else 0} damage photo(s)."
    if field == "confirmSubmit":
        return "Got it, claim confirmed."
    return f"Got your {field_label}."


# ---------------------------------------------------------------------------
# Voice pipeline session
# ---------------------------------------------------------------------------


class VoicePipelineSession:
    """Owns one active /call WebSocket: STT -> LLM -> TTS with barge-in."""

    def __init__(
        self,
        session_id: str,
        store: SessionStore,
        stt: STTProvider,
        llm: LLMProvider,
        tts: TTSProvider,
        send_call: Callable[[str], Any],
        send_chat: Callable[[str], Any],
        close_call: Optional[Callable[[], Any]] = None,
    ):
        self.session_id = session_id
        self.store = store
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self._send_call = send_call
        self._send_chat = send_chat
        self._close_call = close_call

        self._speech_task: Optional[asyncio.Task] = None
        self._agent_speaking = False
        self._user_speaking = False
        self._cancel = False
        self._closed = False
        self._pending_ack: Optional[str] = None
        self._t_user_speech_end: Optional[float] = None

        # Cost tracking (USD).
        self.stt_cost_usd: float = 0.0
        self.llm_cost_usd: float = 0.0
        self.tts_cost_usd: float = 0.0

    # ------------------------------------------------------------------ lifecycle

    async def run(self, ws) -> None:
        """Main loop: receive binary frames from the WebSocket and process them.

        Each frame is handled as a concurrent task so the pipeline can keep
        receiving frames while a TTS turn is streaming (needed for barge-in).
        """
        try:
            while not self._closed:
                data = await ws.receive_bytes()
                asyncio.create_task(self.handle_binary(data))
        except Exception:
            # WebSocketDisconnect or similar; clean up below.
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        self._closed = True
        if self._speech_task and not self._speech_task.done():
            self._speech_task.cancel()
            try:
                await self._speech_task
            except asyncio.CancelledError:
                pass
        if hasattr(self.stt, "close"):
            try:
                await self.stt.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ inbound audio

    async def handle_binary(self, pcm_frame: bytes) -> None:
        events = await self.stt.ingest(pcm_frame)
        for event in events:
            if event.kind == "speech_started":
                self._user_speaking = True
                await self._handle_barge_in()
            elif event.kind == "transcript" and event.is_final and event.text:
                self._user_speaking = False
                await self._handle_final_transcript(event.text)

    # ------------------------------------------------------------------ barge-in

    async def _handle_barge_in(self) -> None:
        if not self._agent_speaking:
            return
        self._cancel = True
        if self._speech_task and not self._speech_task.done():
            self._speech_task.cancel()
        self._agent_speaking = False
        self._cancel = False
        await self._send_call(
            StopPlaybackMessage().model_dump_json(by_alias=True, exclude_none=True)
        )
        self.store.append_event(
            self.session_id, "call", "barge_in", {"reason": "user speech during agent turn"}
        )

    # ------------------------------------------------------------------ cross-channel acknowledgment

    async def notify_chat_field_validated(
        self, field: str, value: Any, state: SessionState
    ) -> None:
        """Called by the chat handler when a field is validated while /call is active.

        If the voice agent is idle, speaks the acknowledgment immediately and
        streams TTS over /call. If a voice turn is active, queues the ack to be
        spoken at the end of the current turn without overlapping audio.
        The ``pending_ack_text`` is always persisted on the session so a fresh
        /call connection can speak it on reconnect.
        """
        ack_text = build_ack_text(field, value, state)
        state.pending_ack_text = ack_text
        self.store.save_session(state)
        if not self._agent_speaking and not self._user_speaking:
            await self._speak(ack_text, state)
        else:
            self._pending_ack = ack_text
            # Persist so a fresh /call connection can speak it on reconnect.
            state.pending_ack_text = ack_text
            self.store.save_session(state)

    # ------------------------------------------------------------------ context-aware greeting

    async def speak_context_greeting(self, state: SessionState) -> None:
        """Speak a context-aware greeting on /call reconnect, referencing
        already-valid fields without re-asking them."""
        valid_fields = [
            f for f, rf in state.fields.items() if rf.status == FieldStatus.VALID
        ]
        if not valid_fields:
            return

        labels = {
            "incidentDescription": "the incident details",
            "incidentLocation": "the location",
            "incidentTime": "the time",
            "injuries": "the injury status",
            "vehicleDrivable": "whether the vehicle is drivable",
            "policyNumber": "your policy number",
            "vehicleType": "the vehicle type",
            "damagePhotos": "the damage photos",
            "email": "your email",
        }
        nice = [labels.get(f, f) for f in valid_fields]
        if len(nice) == 1:
            detail = f"I already have {nice[0]}."
        elif len(nice) == 2:
            detail = f"I already have {nice[0]} and {nice[1]}."
        else:
            detail = f"I already have {', '.join(nice[:-1])} and {nice[-1]}."

        missing = [
            f
            for f in [
                "incidentDescription", "incidentLocation", "incidentTime",
                "injuries", "vehicleDrivable", "policyNumber", "vehicleType",
                "damagePhotos", "email",
            ]
            if state.fields.get(f) is None
            or state.fields.get(f).status != FieldStatus.VALID
        ]
        if missing:
            detail += " Let's finish the remaining items."
        else:
            detail += " Everything looks complete."

        await self._speak(f"Welcome back! {detail}", state)

    # ------------------------------------------------------------------ transcript handling

    async def _handle_final_transcript(self, transcript: str) -> None:
        self._t_user_speech_end = time.monotonic()
        await self._send_call(
            TranscriptMessage(
                role=Role.USER, text=transcript, final=True
            ).model_dump_json(by_alias=True, exclude_none=True)
        )

        state = self.store.get_or_create_session(self.session_id)
        state.engagement.call_seconds = self._elapsed_call_seconds(state)
        summary = self._state_summary(state)
        results = await self.llm.respond(transcript, summary)

        # STT cost: ~$0.000715 per minute of audio (Deepgram Nova-3 rate).
        # Approximate per-transcript charge.
        self.stt_cost_usd += 0.000715 * (len(transcript.split()) / 150.0)
        # LLM cost: ~$0.00019 per 1K tokens for Groq Llama-4-Scout (approx).
        self.llm_cost_usd += 0.00019 * (len(transcript.split()) / 750.0)
        state.cost_usd = self.stt_cost_usd + self.llm_cost_usd + self.tts_cost_usd

        has_tool = any(r.kind == "tool" for r in results)
        has_text = any(r.kind == "text" for r in results)

        for result in results:
            if result.kind == "tool":
                await self._execute_tool(result.tool, result.arguments, state)
            elif result.kind == "text":
                await self._speak(result.text, state)

        # Spoken follow-up if tools were executed without speech and outcome not terminal
        if has_tool and not has_text and state.outcome is None:
            missing_spoken = [
                f for f in [
                    "incidentDescription", "incidentLocation", "incidentTime",
                    "injuries", "vehicleDrivable"
                ]
                if state.fields.get(f) is None or state.fields.get(f).status != FieldStatus.VALID
            ]
            if missing_spoken:
                prompts = {
                    "incidentDescription": "Could you describe what happened during the incident?",
                    "incidentLocation": "Where did this incident occur?",
                    "incidentTime": "When did this happen?",
                    "injuries": "Was anyone injured?",
                    "vehicleDrivable": "Is the vehicle still drivable?",
                }
                follow_up = prompts.get(missing_spoken[0], "Got it. What are the next details?")
            else:
                follow_up = "Got it! I've sent a card to your chat to complete the next step."
            await self._speak(follow_up, state)

        # After tool/text processing, check for a terminal outcome.
        await self._check_outcome(state)

    # ------------------------------------------------------------------ tool execution

    async def _execute_tool(
        self, tool_name: str, arguments: dict, state: SessionState
    ) -> None:
        if tool_name == "record_spoken_fields":
            await self._tool_record_spoken_fields(arguments, state)
        elif tool_name == "request_chat_card":
            await self._tool_request_chat_card(arguments, state)
        elif tool_name == "opt_out_session":
            await self._tool_opt_out(arguments, state)

    async def _tool_record_spoken_fields(
        self, arguments: dict, state: SessionState
    ) -> None:
        kolkata = timezone(timedelta(hours=5, minutes=30))
        now_iso = datetime.now(kolkata).isoformat()
        valid_upload_urls = self.store.get_session_upload_urls(self.session_id)

        for field_name in [
            "incidentDescription",
            "incidentLocation",
            "incidentTime",
            "injuries",
            "vehicleDrivable",
            "email",
            "policyNumber",
        ]:
            if field_name not in arguments or arguments[field_name] is None:
                continue
            ok, err, input_cancel = apply_field_update(
                state,
                field_name,
                arguments[field_name],
                FieldSource.CALL,
                now_iso,
                valid_upload_urls,
            )
            if not ok:
                continue
            self.store.save_session(state)
            self.store.append_event(
                self.session_id,
                "call",
                "field_valid",
                {"field": field_name, "source": "call"},
            )
            if input_cancel is not None:
                await self._send_chat(
                    input_cancel.model_dump_json(by_alias=True, exclude_none=True)
                )

        # Re-evaluate which chat card to open next.
        await self._dispatch_next_chat_card(state)

    async def _tool_request_chat_card(self, arguments: dict, state: SessionState) -> None:
        field = arguments.get("field")
        if not field:
            return
        req = self._build_input_request(field)
        if req is None:
            return
        state.open_request = req
        self.store.save_session(state)
        await self._send_chat(
            req.model_dump_json(by_alias=True, exclude_none=True)
        )

    async def _tool_opt_out(self, arguments: dict, state: SessionState) -> None:
        reason = arguments.get("reason", "user opted out")
        record = evaluate_outcome(
            state, opt_out_reason=reason,
            now_iso=datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat(),
        )
        if record:
            self.store.save_session(state)
            await self._send_call(
                RecordMessage(record=record).model_dump_json(by_alias=True, exclude_none=True)
            )
            await self._send_chat(
                RecordMessage(record=record).model_dump_json(by_alias=True, exclude_none=True)
            )
            await self._deliver(state, record)

    # ------------------------------------------------------------------ speech (TTS)

    async def _speak(self, text: str, state: SessionState) -> None:
        self._agent_speaking = True
        self._cancel = False
        self._speech_task = asyncio.create_task(self._stream_tts(text, state))
        try:
            await self._speech_task
        except asyncio.CancelledError:
            pass
        self._agent_speaking = False
        state.pending_ack_text = None
        self.store.save_session(state)
        # Speak any queued cross-channel acknowledgment after the turn ends.
        if self._pending_ack:
            ack = self._pending_ack
            self._pending_ack = None
            await self._speak(ack, state)

    async def _stream_tts(self, text: str, state: SessionState) -> None:
        t_first_sent: Optional[float] = None
        utterance_id = f"agent_{int(time.time() * 1000)}"

        # Emit initial interim transcript once before chunk streaming starts
        await self._send_call(
            TranscriptMessage(
                role=Role.AGENT, id=utterance_id, text=text, final=False
            ).model_dump_json(by_alias=True, exclude_none=True)
        )

        try:
            async for chunk in self.tts.synthesize(text):
                if self._cancel:
                    break
                if t_first_sent is None:
                    t_first_sent = time.monotonic()
                    ref_time = self._t_user_speech_end if self._t_user_speech_end is not None else t_first_sent
                    turn_latency_ms = (t_first_sent - ref_time) * 1000
                    state.latencies_ms.append(round(turn_latency_ms, 2))
                    self._t_user_speech_end = None
                b64 = base64.b64encode(chunk).decode("ascii")
                await self._send_call(
                    AudioMessage(sample_rate=24000, data=b64).model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                )
        finally:
            # Mark the transcript final once streaming ends (or is cancelled).
            await self._send_call(
                TranscriptMessage(
                    role=Role.AGENT, id=utterance_id, text=text, final=True
                ).model_dump_json(by_alias=True, exclude_none=True)
            )
            # TTS cost: ~$0.00001 per character (approximate Deepgram Aura rate).
            self.tts_cost_usd += len(text) * 0.00001
            state.cost_usd = self.stt_cost_usd + self.llm_cost_usd + self.tts_cost_usd
            self.store.save_session(state)

    # ------------------------------------------------------------------ chat card dispatch

    async def _dispatch_next_chat_card(self, state: SessionState) -> None:
        call_active = True
        req = next_missing_input_request(state, call_active=call_active)
        if req is not None:
            state.open_request = req
            self.store.save_session(state)
            await self._send_chat(
                req.model_dump_json(by_alias=True, exclude_none=True)
            )

    def _build_input_request(self, field: str) -> Optional[InputRequest]:
        from .validator import _build_card_spec

        return _build_card_spec(field)

    # ------------------------------------------------------------------ outcome & close

    async def _check_outcome(self, state: SessionState) -> None:
        record = evaluate_outcome(state)
        if record:
            self.store.save_session(state)
            await self._send_call(
                RecordMessage(record=record).model_dump_json(by_alias=True, exclude_none=True)
            )
            await self._send_chat(
                RecordMessage(record=record).model_dump_json(by_alias=True, exclude_none=True)
            )
            await self._deliver(state, record)
            # Close the call WebSocket on terminal outcome (auto-hangup).
            self._closed = True
            if self._close_call is not None:
                try:
                    await self._close_call()
                except Exception:
                    pass

    async def _deliver(self, state: SessionState, record) -> None:
        from .webhook import deliver_final_record

        delivered = deliver_final_record(self.store, state, record)
        # Mirror to the webhook sink if one is attached to the manager.

    # ------------------------------------------------------------------ helpers

    def _state_summary(self, state: SessionState) -> str:
        valid = [
            f for f, rf in state.fields.items()
            if rf.status == FieldStatus.VALID
        ]
        missing = [
            f for f in [
                "incidentDescription", "incidentLocation", "incidentTime",
                "injuries", "vehicleDrivable", "policyNumber", "vehicleType",
                "damagePhotos",
            ]
            if state.fields.get(f) is None or state.fields.get(f).status != FieldStatus.VALID
        ]
        return (
            f"valid: {', '.join(valid) if valid else 'none'}; "
            f"missing: {', '.join(missing) if missing else 'none'}; "
            f"injury_review_flag={state.injury_review_flag}; "
            f"confirm_rejections={state.confirm_rejections}"
        )

    def _elapsed_call_seconds(self, state: SessionState) -> int:
        return state.engagement.call_seconds
