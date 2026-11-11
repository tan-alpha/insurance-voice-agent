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
import inspect
import json
import os
import re
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
from .validator import (
    SPOKEN_ORDER,
    apply_field_update,
    evaluate_outcome,
    next_missing_input_request,
    validate_field,
)

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
        self._keepalive_task: Optional[asyncio.Task] = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[STTEvent] = asyncio.Queue()

    async def _connect(self) -> None:
        import websockets  # type: ignore

        url = (
            "wss://api.deepgram.com/v1/listen?"
            "model=nova-3&encoding=linear16&sample_rate=16000&channels=1"
            "&interim_results=true&utterance_end_ms=1000&vad_events=true"
            "&endpointing=500&numerals=true&smart_format=true"
        )
        self._ws = await websockets.connect(
            url, additional_headers={"Authorization": f"Token {self.api_key}"}
        )
        ws = self._ws
        self._connect_task = asyncio.create_task(self._reader(ws))
        self._keepalive_task = asyncio.create_task(self._keepalive(ws))

    async def _reader(self, ws: Any) -> None:
        try:
            async for message in ws:
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
        except Exception:
            pass
        finally:
            if self._ws is ws:
                self._ws = None

    async def _keepalive(self, ws: Any) -> None:
        while self._ws is ws:
            try:
                await asyncio.sleep(5)
                if self._ws is not ws:
                    break
                async with self._send_lock:
                    await ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:
                break

    async def ingest(self, pcm_frame: bytes) -> List[STTEvent]:
        if self._ws is None:
            async with self._connect_lock:
                if self._ws is None:
                    await self._connect()
        if self._ws is not None:
            try:
                async with self._send_lock:
                    await self._ws.send(pcm_frame)
            except Exception:
                self._ws = None
        events: List[STTEvent] = []
        while not self._event_queue.empty():
            events.append(self._event_queue.get_nowait())
        return events

    async def close(self) -> None:
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._ws:
            ws = self._ws
            self._ws = None
            try:
                await ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Production: Groq tool-calling LLM
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Production: OpenAI-compatible LLM (OpenRouter / Groq / etc.)
# ---------------------------------------------------------------------------


def _get_loop_client(
    clients: dict[int, Any], timeout: float = 30.0, keepalive_expiry: float = 5.0
) -> Any:
    """Return a cached keep-alive httpx.AsyncClient bound to the running event loop."""
    import httpx  # type: ignore

    loop_id = id(asyncio.get_running_loop())
    client = clients.get(loop_id)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                keepalive_expiry=keepalive_expiry,
            ),
        )
        clients[loop_id] = client
    return client


async def _reset_loop_client(clients: dict[int, Any]) -> None:
    """Close and evict the cached httpx.AsyncClient for the running event loop."""
    loop_id = id(asyncio.get_running_loop())
    client = clients.pop(loop_id, None)
    if client is not None:
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:
            pass


async def _close_loop_clients(clients: dict[int, Any]) -> None:
    """Close any cached httpx.AsyncClient instances on the current loop or all loops."""
    for client in list(clients.values()):
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:
            pass
    clients.clear()


def _parse_tool_args(raw: Any) -> dict:
    """Parse tool-call arguments, recovering from minor LLM JSON glitches."""
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*,+", ",", raw)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned).lstrip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(cleaned)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


class OpenAILLM(LLMProvider):
    """OpenAI-compatible chat completions (OpenRouter, Groq, etc.)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1/chat/completions",
        model: str = "cohere/north-mini-code:free",
    ):
        self.api_key = api_key
        base = base_url.rstrip("/")
        if not base.endswith("/chat/completions"):
            base = f"{base}/chat/completions"
        self.base_url = base
        self.model = model
        self._clients: dict[int, Any] = {}

    async def aclose(self) -> None:
        """Close cached HTTP clients held by this provider."""
        await _close_loop_clients(self._clients)

    def _tools(self) -> List[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "record_spoken_fields",
                    "description": (
                        "Record ONLY claim fields explicitly stated by the user. "
                        "NEVER guess, assume, or default unmentioned fields."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "incidentDescription": {
                                "type": "string",
                                "description": "What happened ONLY if explicitly stated (e.g. 'Car accident on empty road').",
                            },
                            "incidentLocation": {
                                "type": "string",
                                "description": "Where the incident occurred ONLY if explicitly stated (e.g. 'MG Road').",
                            },
                            "incidentTime": {
                                "type": "string",
                                "description": "When the incident happened ONLY if explicitly stated, e.g. '2 hours ago', 'today at 10am'.",
                            },
                            "injuries": {
                                "type": "string",
                                "description": (
                                    "Injuries ONLY if the user explicitly stated whether anyone was hurt "
                                    "('none' ONLY if user said nobody was hurt). Omit completely if not mentioned."
                                ),
                            },
                            "vehicleDrivable": {
                                "type": "boolean",
                                "description": (
                                    "Boolean ONLY if the user explicitly stated whether the car can be driven "
                                    "or was towed. Omit completely if not mentioned."
                                ),
                            },
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
            "You are a concise motor insurance claims voice assistant on a live phone call. "
            f"Current session state: {state_summary}. "
            "ALWAYS call record_spoken_fields when the user provides any spoken claim details "
            "(incidentDescription, incidentLocation, incidentTime, injuries, vehicleDrivable). "
            "Record ONLY new or updated fields the user explicitly stated in their latest turn; "
            "do NOT re-send or overwrite already collected fields unless the user explicitly corrects them. "
            "NEVER guess, infer, or fill default values "
            "for unmentioned fields (especially injuries or vehicleDrivable). "
            "Ask ONLY for the single next missing spoken field in 1-2 short plain spoken sentences. "
            "Never output markdown, bullet points, numbered lists, or repeat the welcome greeting. "
            "Do NOT call request_chat_card until ALL 5 spoken fields "
            "(incidentDescription, incidentLocation, incidentTime, injuries, vehicleDrivable) are valid. "
            "Once all 5 spoken fields are valid, do not ask more spoken questions; let the user proceed with the chat prompts. "
            "Use opt_out_session only if the user explicitly asks to cancel the claim."
        )

    async def respond(
        self,
        transcript: str,
        state_summary: str,
        history: Optional[List[dict]] = None,
    ) -> List[LLMResult]:
        messages: List[dict] = [
            {"role": "system", "content": self._system_prompt(state_summary)},
        ]
        if history:
            messages.extend(history[-10:])
        messages.append({"role": "user", "content": transcript})
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "tools": self._tools(),
            "tool_choice": "auto",
            "temperature": 0.0,
        }
        # Nemotron models need chat_template_kwargs to suppress reasoning.
        if any(k in self.model.lower() for k in ("nemotron", "llama-4-scout")):
            payload["chat_template_kwargs"] = {
                "enable_thinking": False,
                "force_nonempty_content": True,
            }
        results: List[LLMResult] = []
        import httpx  # type: ignore

        data: dict = {}
        for attempt in range(2):
            client = _get_loop_client(self._clients, timeout=30.0)
            try:
                resp = await client.post(
                    self.base_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.RemoteProtocolError, httpx.TransportError):
                await _reset_loop_client(self._clients)
                if attempt == 1:
                    raise
        choice = data["choices"][0]["message"]
        if choice.get("tool_calls"):
            for tc in choice["tool_calls"]:
                fn = tc["function"]
                args = _parse_tool_args(fn.get("arguments", "{}"))
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
        self._clients: dict[int, Any] = {}

    async def aclose(self) -> None:
        """Close cached HTTP clients held by this provider."""
        await _close_loop_clients(self._clients)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        import httpx  # type: ignore

        for attempt in range(2):
            client = _get_loop_client(self._clients, timeout=30.0)
            yielded = False
            try:
                async with client.stream(
                    "POST",
                    self._base_url,
                    headers={"Authorization": f"Token {self.api_key}"},
                    content=text,
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yielded = True
                            yield chunk
                return
            except (httpx.RemoteProtocolError, httpx.TransportError):
                await _reset_loop_client(self._clients)
                if attempt == 1 or yielded:
                    raise


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

        # Call duration tracking.
        self._call_started_at: float = time.monotonic()
        self._initial_call_seconds: Optional[int] = None

        # Per-loop turn serialization & rolling short turn history.
        self._turn_locks: dict[int, asyncio.Lock] = {}
        self._history: List[dict] = []

        # Timing & playback tracking for diagnostics and non-clipping hangup.
        self._closing_handoff: bool = False
        self._speech_cancelled: bool = False
        self._last_llm_ms: float = 0.0
        self._last_tts_bytes: int = 0
        self._last_tts_elapsed_s: float = 0.0
        self._last_tts_play_elapsed_s: float = 0.0

    def _get_turn_lock(self) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        lock = self._turn_locks.get(loop_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[loop_id] = lock
        return lock

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
        try:
            state = self.store.get_or_create_session(self.session_id)
            state.engagement.call_seconds = self._elapsed_call_seconds(state)
            self.store.save_session(state)
        except Exception:
            pass
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
        if not self._agent_speaking or self._closing_handoff:
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
        self._user_speaking = False
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
        async with self._get_turn_lock():
            self._t_user_speech_end = time.monotonic()
            await self._send_call(
                TranscriptMessage(
                    role=Role.USER, text=transcript, final=True
                ).model_dump_json(by_alias=True, exclude_none=True)
            )

            state = self.store.get_or_create_session(self.session_id)
            state.engagement.call_seconds = self._elapsed_call_seconds(state)
            summary = self._state_summary(state)
            t_llm_start = time.monotonic()
            if isinstance(self.llm, OpenAILLM):
                results = await self.llm.respond(transcript, summary, history=self._history)
            else:
                results = await self.llm.respond(transcript, summary)
            self._last_llm_ms = (time.monotonic() - t_llm_start) * 1000.0

            # Reload fresh state after async LLM response so concurrent updates are preserved.
            state = self.store.get_or_create_session(self.session_id)
            state.engagement.call_seconds = self._elapsed_call_seconds(state)

            # STT cost: ~$0.000715 per minute of audio (Deepgram Nova-3 rate).
            # Approximate per-transcript charge.
            self.stt_cost_usd += 0.000715 * (len(transcript.split()) / 150.0)
            # LLM cost: ~$0.00019 per 1K tokens for Groq Llama-4-Scout (approx).
            self.llm_cost_usd += 0.00019 * (len(transcript.split()) / 750.0)
            state.cost_usd = self.stt_cost_usd + self.llm_cost_usd + self.tts_cost_usd

            def _all_spoken_valid(st: SessionState) -> bool:
                return all(
                    st.fields.get(f) is not None and st.fields.get(f).status == FieldStatus.VALID
                    for f in SPOKEN_ORDER
                )

            had_all_spoken = _all_spoken_valid(state)
            recorded_spoken_tool = any(
                r.kind == "tool" and r.tool == "record_spoken_fields" for r in results
            )
            last_prompt = next(
                (m.get("content", "") for m in reversed(self._history) if m.get("role") == "assistant"),
                "",
            )
            next_expected = next(
                (
                    f for f in SPOKEN_ORDER
                    if state.fields.get(f) is None or state.fields.get(f).status != FieldStatus.VALID
                ),
                None,
            )

            for result in results:
                if result.kind == "tool":
                    await self._execute_tool(
                        result.tool,
                        result.arguments,
                        state,
                        dispatch_card=False,
                        user_transcript=transcript,
                        last_prompt=last_prompt,
                        next_expected=next_expected,
                    )

            if state.outcome is None and await self._recover_direct_prompt_answer(
                transcript, last_prompt, state
            ):
                recorded_spoken_tool = True

            has_all_spoken_now = _all_spoken_valid(state)
            just_finished_spoken = (not had_all_spoken) and has_all_spoken_now

            spoken_reply: Optional[str] = None
            if just_finished_spoken and state.outcome is None:
                spoken_reply = (
                    "Thank you! I've collected the incident details from the call. "
                    "Ending the call now so you can finish the remaining details in chat."
                )
            else:
                text_parts = [
                    r.text.strip()
                    for r in results
                    if r.kind == "text" and r.text and r.text.strip()
                ]
                if text_parts:
                    spoken_reply = " ".join(text_parts)
                elif state.outcome is None:
                    missing_spoken = [
                        f for f in SPOKEN_ORDER
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
                        spoken_reply = prompts.get(missing_spoken[0], "Got it. What are the next details?")
                    else:
                        spoken_reply = (
                            "Please continue using the chat prompts I've sent to complete your claim."
                        )

            self._history.append({"role": "user", "content": transcript})
            if just_finished_spoken and state.outcome is None:
                self._closing_handoff = True
            if spoken_reply:
                self._history.append({"role": "assistant", "content": spoken_reply})
                self._history = self._history[-12:]
                await self._speak(spoken_reply, state)
            else:
                self._history = self._history[-12:]

            # After tool/text processing, check for a terminal outcome.
            await self._check_outcome(state)
            if just_finished_spoken and not self._closed and not self._speech_cancelled:
                if self._close_call is not None:
                    await self._finish_call_after_speech()
                else:
                    await self._dispatch_next_chat_card(state)
            elif recorded_spoken_tool and not just_finished_spoken and state.outcome is None:
                await self._dispatch_next_chat_card(state)

    # ------------------------------------------------------------------ tool execution

    async def _execute_tool(
        self,
        tool_name: str,
        arguments: dict,
        state: SessionState,
        dispatch_card: bool = True,
        user_transcript: Optional[str] = None,
        last_prompt: str = "",
        next_expected: Optional[str] = None,
    ) -> None:
        if tool_name == "record_spoken_fields":
            await self._tool_record_spoken_fields(
                arguments,
                state,
                dispatch_card=dispatch_card,
                user_transcript=user_transcript,
                last_prompt=last_prompt,
                next_expected=next_expected,
            )
        elif tool_name == "request_chat_card":
            await self._tool_request_chat_card(arguments, state)
        elif tool_name == "opt_out_session":
            await self._tool_opt_out(arguments, state)

    @staticmethod
    def _is_spoken_field_grounded(
        field_name: str,
        transcript: str,
        last_prompt: str,
        next_expected: Optional[str],
    ) -> bool:
        """Prevent hallucinated defaults for injuries/vehicleDrivable when unmentioned."""
        t = transcript.lower()
        p = last_prompt.lower()
        if field_name == "injuries":
            injury_kw = (
                "injur", "hurt", "pain", "hospital", "doctor", "ambulance",
                "medic", "nobody", "no one", "none", "unharm", "safe", "fine",
                "okay", "bruise", "bleed", "fracture", "broken", "whiplash",
                "concussion", "nil", "scratch", "ache", "sore", "wound",
            )
            return (
                next_expected == "injuries"
                or any(k in p for k in ("injur", "hurt"))
                or any(k in t for k in injury_kw)
            )
        if field_name == "vehicleDrivable":
            drivable_kw = (
                "driv", "tow", "undriv", "totaled", "wreck", "cannot move",
                "can't move", "not moving", "not running", "won't start",
                "runs fine", "still runs", "won't run", "inoperable", "move",
            )
            return (
                next_expected == "vehicleDrivable"
                or any(k in p for k in ("driv", "tow"))
                or any(k in t for k in drivable_kw)
            )
        return True

    async def _apply_call_field(
        self,
        state: SessionState,
        field_name: str,
        raw_value: Any,
        now_iso: str,
        valid_upload_urls: set[str],
    ) -> bool:
        ok, err, input_cancel = apply_field_update(
            state,
            field_name,
            raw_value,
            FieldSource.CALL,
            now_iso,
            valid_upload_urls,
        )
        if not ok:
            return False
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
        return True

    async def _recover_direct_prompt_answer(
        self,
        transcript: str,
        last_prompt: str,
        state: SessionState,
    ) -> bool:
        """Recover direct yes/no answers when the LLM drops a tool call."""
        p = last_prompt.lower()
        t_clean = transcript.strip().lower().rstrip(".!?")
        kolkata = timezone(timedelta(hours=5, minutes=30))
        now_iso = datetime.now(kolkata).isoformat()
        valid_upload_urls = self.store.get_session_upload_urls(self.session_id)

        rf_drv = state.fields.get("vehicleDrivable")
        if (
            (rf_drv is None or rf_drv.status != FieldStatus.VALID)
            and any(k in p for k in ("driv", "tow"))
        ):
            ok, _, _ = validate_field(
                "vehicleDrivable", transcript, FieldSource.CALL, now_iso, valid_upload_urls
            )
            if ok:
                return await self._apply_call_field(
                    state, "vehicleDrivable", transcript, now_iso, valid_upload_urls
                )

        rf_inj = state.fields.get("injuries")
        if (
            (rf_inj is None or rf_inj.status != FieldStatus.VALID)
            and any(k in p for k in ("injur", "hurt"))
            and t_clean not in ("yes", "yeah", "yep", "ok", "okay", "sure")
        ):
            ok, norm, _ = validate_field(
                "injuries", transcript, FieldSource.CALL, now_iso, valid_upload_urls
            )
            injury_words = (
                "pain", "hurt", "injur", "fracture", "bleed", "hospital", "doctor",
                "whiplash", "concussion", "bruise", "broken", "sore", "ache",
                "wound", "minor", "major", "scrape", "cut", "neck", "head", "arm", "leg", "back",
            )
            if ok and (norm == "none" or any(w in transcript.lower() for w in injury_words)):
                return await self._apply_call_field(
                    state, "injuries", transcript, now_iso, valid_upload_urls
                )

        return False

    async def _tool_record_spoken_fields(
        self,
        arguments: dict,
        state: SessionState,
        dispatch_card: bool = True,
        user_transcript: Optional[str] = None,
        last_prompt: str = "",
        next_expected: Optional[str] = None,
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
            existing = state.fields.get(field_name)
            if (
                user_transcript is not None
                and existing is not None
                and existing.status == FieldStatus.VALID
                and existing.source == FieldSource.CALL
            ):
                corr_kw = (
                    "actually", "correction", "sorry", "meant to say",
                    "instead", "change my", "wrong",
                )
                prompt_kws = {
                    "incidentDescription": ("what happened", "incident", "describe"),
                    "incidentLocation": ("where", "location", "place"),
                    "incidentTime": ("when", "time"),
                    "injuries": ("injur", "hurt"),
                    "vehicleDrivable": ("driv", "tow"),
                }
                p_low = last_prompt.lower()
                asked_same = any(k in p_low for k in prompt_kws.get(field_name, ()))
                if not asked_same and not any(k in user_transcript.lower() for k in corr_kw):
                    continue
            if user_transcript is not None and not self._is_spoken_field_grounded(
                field_name, user_transcript, last_prompt, next_expected
            ):
                continue
            await self._apply_call_field(
                state, field_name, arguments[field_name], now_iso, valid_upload_urls
            )

        # Optionally re-evaluate which chat card to open next.
        if dispatch_card:
            await self._dispatch_next_chat_card(state)

    async def _tool_request_chat_card(self, arguments: dict, state: SessionState) -> None:
        if any(
            state.fields.get(f) is None or state.fields.get(f).status != FieldStatus.VALID
            for f in SPOKEN_ORDER
        ):
            return
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
        self._speech_cancelled = False
        self._speech_task = asyncio.create_task(self._stream_tts(text, state))
        try:
            await self._speech_task
        except asyncio.CancelledError:
            self._speech_cancelled = True
        except Exception as exc:
            print(f"[TTS_ERROR] session={self.session_id} err={exc}")
        finally:
            self._agent_speaking = False
            self._closing_handoff = False
        state.pending_ack_text = None
        self.store.save_session(state)
        # Speak any queued cross-channel acknowledgment after the turn ends.
        if self._pending_ack:
            ack = self._pending_ack
            self._pending_ack = None
            await self._speak(ack, state)

    async def _stream_tts(self, text: str, state: SessionState) -> None:
        t_tts_start = time.monotonic()
        t_first_sent: Optional[float] = None
        total_audio_bytes = 0
        self._last_tts_bytes = 0
        self._last_tts_elapsed_s = 0.0
        self._last_tts_play_elapsed_s = 0.0
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
                    self._speech_cancelled = True
                    break
                total_audio_bytes += len(chunk)
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
            elapsed_s = time.monotonic() - t_tts_start
            self._last_tts_bytes = total_audio_bytes
            self._last_tts_elapsed_s = elapsed_s
            self._last_tts_play_elapsed_s = (
                (time.monotonic() - t_first_sent)
                if t_first_sent is not None
                else 0.0
            )
            tts_first_ms = (
                ((t_first_sent - t_tts_start) * 1000.0)
                if t_first_sent is not None
                else 0.0
            )
            print(
                f"[TIMING] session={self.session_id} llm_ms={self._last_llm_ms:.0f} "
                f"tts_first_ms={tts_first_ms:.0f} tts_total_ms={elapsed_s * 1000.0:.0f} "
                f"audio_bytes={total_audio_bytes} cancelled={self._cancel or self._speech_cancelled}"
            )
            self._last_llm_ms = 0.0
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
            await self._invoke_close_call()

    async def _invoke_close_call(self) -> None:
        self._closed = True
        if self._close_call is not None:
            try:
                res = self._close_call()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass

    async def _finish_call_after_speech(self) -> None:
        """Wait for remaining streamed 24 kHz PCM16 playback before closing /call."""
        audio_duration_s = self._last_tts_bytes / 48000.0
        remaining_s = max(0.0, audio_duration_s - self._last_tts_play_elapsed_s)
        if audio_duration_s > 0.1:
            remaining_s += 1.5
        if remaining_s > 0:
            await asyncio.sleep(remaining_s)
        if not self._speech_cancelled and not self._closed:
            await self._invoke_close_call()

    async def _deliver(self, state: SessionState, record) -> None:
        from .webhook import deliver_final_record

        delivered = deliver_final_record(self.store, state, record)
        # Mirror to the webhook sink if one is attached to the manager.

    # ------------------------------------------------------------------ helpers

    def _state_summary(self, state: SessionState) -> str:
        collected = [
            f"{f}={repr(str(rf.value)[:80]) if rf.value is not None else 'skipped'}"
            for f, rf in state.fields.items()
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
            f"collected (do not overwrite): {', '.join(collected) if collected else 'none'}; "
            f"missing: {', '.join(missing) if missing else 'none'}; "
            f"injury_review_flag={state.injury_review_flag}; "
            f"confirm_rejections={state.confirm_rejections}"
        )

    def _elapsed_call_seconds(self, state: SessionState) -> int:
        if self._initial_call_seconds is None:
            self._initial_call_seconds = state.engagement.call_seconds
        return self._initial_call_seconds + max(1, int(time.monotonic() - self._call_started_at))
