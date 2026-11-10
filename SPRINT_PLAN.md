# SPRINT_PLAN.md — Multichannel Voice + Chat AI Agent (Ground-Truth Locked)

## 0. Ground-Truth Codebase Anchors (`starter/src/`)
**STRICT RULE:** `starter/` is used 100% unmodified. Zero API keys, tokens, or secrets in browser code or git history.

1. **WebSocket & HTTP Endpoints:**
   - `WS /chat?sessionId={sessionId}` (`starter/src/hooks/useChat.ts`, auto-connects on mount, exponential backoff 1s -> 15s).
   - `WS /call?sessionId={sessionId}` (`starter/src/hooks/useCall.ts`, connects on Call click; does not auto-reconnect on close).
   - `POST /sessions/{sessionId}/files` (`starter/src/transport/upload.ts#L11-L25`, `multipart/form-data` field `"file"` -> returns HTTP 200 `{"url": "..."}`).
   - `GET /files/{sessionId}/{filename}` (serves uploaded files referenced by `FinalRecord`).

2. **Audio & Backpressure Constants (`starter/src/protocol/constants.ts`):**
   - Inbound `/call` binary frames: `MIC_SAMPLE_RATE = 16000` Hz (`#L2`), `MIC_FRAME_MS = 20` ms, `MIC_FRAME_BYTES = 640` bytes (`#L4`), raw PCM16 LE mono `ArrayBuffer`. Client drops frames if `ws.bufferedAmount >= 16000` (`useCall.ts#L12-L14`).
   - Outbound `/call` audio JSON: `DEFAULT_PLAYBACK_RATE = 24000` Hz (`#L7`), base64-encoded PCM16 LE mono inside `AudioMessage`.
   - Immediate barge-in flush: `{"type": "stopPlayback"}` halts all active `AudioBufferSourceNode` instances (`starter/src/audio/playback.ts`).

3. **Client-Side Validation Bypass for Server `inputError` Testing (`starter/src/lib/answers.ts#L50-L51`, `Composer.tsx#L31-L36`):**
   - `Composer.tsx` runs `textAnswer(spec, text, inputRef.current)` before invoking `onAnswer()`. If `spec.pattern` is set on `InputRequest`, mismatches are blocked in the browser and never sent over `/chat`.
   - **Hardened Rule:** All server-dispatched `InputRequest` payloads omit `pattern` (`pattern=None`) and use `kind="text"` for `policyNumber` so invalid strings (e.g., `"POL-123"`) reach FastAPI, allowing deterministic server validation, `inputError` card re-opening, and 3-strike `needsReview` escalation.

4. **Dynamic Record Rendering (`starter/src/components/record/FieldList.tsx#L7-L19`, `RecordCard.tsx#L6-L22`):**
   - `FieldList.tsx` iterates `Object.entries(fields)` dynamically and humanizes keys via `fieldLabel(name)` (`starter/src/lib/record.ts#L14-L15`).
   - `RecordCard.tsx` renders the full `FinalRecord` (including `conflicts`, `engagement`, `costUsd`, `endedAt`) inside its expandable `<pre>` JSON block (`#L19-L22`).

---

## 1. Exact Wire Protocol Schemas (`backend/app/protocol.py`)

All Pydantic v2 models use `ConfigDict(populate_by_name=True, extra="forbid")` and serialize with `by_alias=True`, `exclude_none=True`.

### 1.1 Enums (Mirroring `starter/src/protocol/enums.ts`)
- `Role`: `"user"` | `"agent"` (`#L1-L4`)
- `CallServerMessageType`: `"audio"` | `"stopPlayback"` | `"transcript"` (`#L7-L9`)
- `ChatClientMessageType`: `"inputResponse"` (`#L20-L22`)
- `InputKind`: `"text"` | `"email"` | `"phone"` | `"number"` | `"date"` | `"select"` | `"multiSelect"` | `"file"` | `"confirm"` (`#L24-L34`)
- `Outcome`: `"completed"` | `"partial"` | `"optedOut"` | `"needsReview"` (`#L36-L41`)
- `FieldSource`: `"call"` | `"chat"` (`#L43-L46`)
- `FieldStatus`: `"valid"` | `"missing"` | `"invalid"` (`#L48-L52`)

### 1.2 Server -> Client Messages (`starter/src/protocol/types.ts`)
- **`AudioMessage` (`#L12-L16`):** `{"type": "audio", "sampleRate": 24000, "data": "<base64>"}`
- **`StopPlaybackMessage` (`#L18-L20`):** `{"type": "stopPlayback"}`
- **`TranscriptMessage` (`#L22-L28`):** `{"type": "transcript", "role": "user"|"agent", "text": "...", "final": bool, "id"?: "..."}`
- **`ChatMessage` (`#L32-L37`):** `{"type": "message", "role": "user"|"agent", "text": "...", "id"?: "..."}`
- **`InputOption` (`#L39-L42`):** `{"value": "...", "label"?: "..."}`
- **`InputRequest` (`#L44-L57`):** `{"type": "inputRequest", "id": "...", "field": "...", "kind": InputKind, "label": "...", "help"?: "...", "placeholder"?: "...", "required"?: bool, "pattern"?: None, "options"?: list[InputOption], "accept"?: "...", "multiple"?: bool}`
- **`InputError` (`#L59-L63`):** `{"type": "inputError", "requestId": "...", "message"?: "..."}`
- **`InputCancel` (`#L65-L69`):** `{"type": "inputCancel", "requestId": "...", "reason"?: "..."}`
- **`RecordMessage` (`#L71-L74`):** `{"type": "record", "record": FinalRecord}`

### 1.3 Client -> Server Message (`starter/src/protocol/types.ts#L80-L85`)
- **`InputResponse`:** `{"type": "inputResponse", "requestId": "...", "field": "...", "value": str | int | float | bool | list[str] | None}`

### 1.4 FinalRecord & Sub-Models (`starter/src/protocol/types.ts#L91-L116`)
- **`RecordField` (`#L91-L96`):** `{"value": Any, "source": "call"|"chat"|None, "collectedAt": str|None, "status": "valid"|"missing"|"invalid"}`
- **`RecordConflict` (`#L98-L104`):** `{"field": str, "callValue": Any, "chatValue": Any, "resolution": str, "reason": str}`
- **`EngagementMetrics`:** `{"calls": int, "callSeconds": int, "chatMessages": int}`
- **`FinalRecord` (`#L106-L116`):**
  ```json
  {
    "sessionId": "str",
    "revision": 1,
    "outcome": "completed | partial | optedOut | needsReview",
    "outcomeReason": "str",
    "fields": {"<field>": RecordField},
    "conflicts": [RecordConflict],
    "engagement": {"calls": 1, "callSeconds": 120, "chatMessages": 5},
    "costUsd": 0.0142,
    "endedAt": "2026-11-10T19:15:00+05:30"
  }

```

---

## 2. Canonical Domain Scenario: Motor Insurance Claim

### 2.1 Ordered Field Specifications (8 Required + 1 Optional + 1 Confirmation Control)

All `collectedAt` and `endedAt` timestamps are generated in `Asia/Kolkata` (`+05:30`) ISO-8601 format.

| # | Field Key | Channel Primary | `InputKind` | Required | Normalization & Deterministic Validation Rule (`validator.py`) |
| --- | --- | --- | --- | --- | --- |
| 1 | `incidentDescription` | Call (or Chat fallback) | `text` | Yes | Strip & collapse whitespace. Valid if `len(val) >= 10` and contains at least 2 words. |
| 2 | `incidentLocation` | Call (or Chat fallback) | `text` | Yes | Strip & collapse whitespace. Valid if `len(val) >= 3`. |
| 3 | `incidentTime` | Call (or Chat fallback) | `text` | Yes | Parse ISO-8601 or natural expressions (`"today at 4pm"`, `"yesterday morning"`, `"2 hours ago"`, `YYYY-MM-DD HH:MM`) into ISO-8601 `+05:30`. Reject if in the future (`> now + 5 min`) or `> 365 days` in the past. |
| 4 | `injuries` | Call (or Chat fallback) | `text` | Yes | Normalize `"no"`, `"none"`, `"no one hurt"`, `"nobody"` -> `"none"`. Valid if `"none"` or `len(val) >= 3`. **If valid and `!= "none"`, set `session.injury_review_flag = True`.** |
| 5 | `vehicleDrivable` | Call (or Chat fallback) | `confirm` / bool | Yes | Normalize bool or `"yes"`/`"true"`/`"drivable"` -> `True`, `"no"`/`"false"`/`"not drivable"`/`"towed"` -> `False`. |
| 6 | `policyNumber` | Chat Card | `text` | Yes | `.strip().upper()` (also strip spaces/hyphens if user spoke `"POL 123456"` -> `"POL-123456"`). Valid iff matches `^POL-[0-9]{6}$`. |
| 7 | `vehicleType` | Chat Card | `select` | Yes | Options: `[{"value": "car", "label": "Car / SUV"}, {"value": "twoWheeler", "label": "Two-Wheeler"}, {"value": "commercial", "label": "Commercial Vehicle"}]`. Case-insensitive match normalized to exact enum value. |
| 8 | `damagePhotos` | Chat Card | `file` | Yes | `accept="image/*"`, `multiple=True`. Valid iff `isinstance(val, list)` and `1 <= len(val) <= 5` and every URL belongs to a verified image uploaded in this `sessionId`. |
| 9 | `email` | Chat Card or Spoken | `text` | **Optional** | `.strip().lower()`. Spoken `"at"`/`"dot"` normalized on `source="call"`. Valid iff matches `^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$` (or `"skip"` / empty in chat marks `skipped=True` and advances without error). *(Uses `kind="text"` so invalid emails reach backend `validator.py` instead of HTML5 `<input type="email">` blocking).* |
| 10 | `confirmSubmit` | Chat Card | `confirm` | Control | Dispatched only after fields 1–8 are `"valid"` (and `email` is either `"valid"` or offered/skipped). `True` finalizes claim; `False` asks what to change (second `False` triggers `optedOut`). |

### 2.2 Conflict Resolution Matrix (`validator.py`)

Triggered when a field already has `status == "valid"` from `source_A` and receives a new `valid` value from `source_B` (`source_A != source_B`) where `normalized_new != existing_value`:

* **Structured Fields (`policyNumber`, `vehicleType`, `email`, `damagePhotos`):**
* `chat` always wins over `call`.
* Appends `RecordConflict(field=field, callValue=call_val, chatValue=chat_val, resolution="chatValue", reason="Typed or uploaded chat input preferred over spoken ASR transcript")`.


* **Conversational Fields (`incidentDescription`, `incidentLocation`, `incidentTime`, `injuries`, `vehicleDrivable`):**
* Latest valid update wins (`resolution="chatValue"` if updated via chat, `"callValue"` if corrected via call).
* Appends `RecordConflict` with explicit `reason="User explicitly updated value in subsequent channel turn"`.



### 2.3 Deterministic Outcome & Revision State Machine

1. **`optedOut`**:
* User explicitly asks to cancel/stop on call (`opt_out_session` tool) or types `"cancel claim"` / `"opt out"` in chat, OR submits `confirmSubmit == False` twice.


2. **`needsReview`**:
* **Immediate Trigger:** Any field accumulates `>= 3` consecutive validation failures (`failure_counts[field] >= 3`), OR vision check flags uploaded photo as non-vehicle/unreadable twice.
* **Post-Confirm Trigger:** All 8 required fields are `valid` and `confirmSubmit == True`, AND `fields["injuries"].value != "none"` (`outcomeReason="Bodily injury reported; requires human adjuster review"`).


3. **`completed`**:
* All 8 required fields have `status == "valid"`, `confirmSubmit == True`, and no `needsReview` condition is active (`outcomeReason="All required claim fields validated and confirmed by user"`).


4. **`partial`**:
* Background inactivity timer expires (`PARTIAL_TIMEOUT_SECONDS`, default `120s`) while both `/call` WS is disconnected and no `/chat` message/input has arrived, and at least one required field is still `"missing"` or `"invalid"` (or `confirmSubmit` not yet confirmed).


5. **Revision Lifecycle (`revision` & `delivered_revision`):**
* Starts at `revision = 0`, `delivered_revision = 0`.
* Transitioning to any outcome (`partial`, `completed`, `needsReview`, `optedOut`) increments `revision += 1`, saves `FinalRecord` to SQLite, sends `{"type": "record", "record": ...}` on `/chat`, and triggers `webhook.py`.
* `webhook.py` checks `if session.delivered_revision < session.revision:` before POSTing, then atomically updates `delivered_revision = session.revision`.
* If a `partial` session (`revision == 1`) later receives user input on `/chat` or `/call` and reaches `completed` / `needsReview` / `optedOut`, `revision` increments to `2` and delivers the upgraded record once.



---

## 3. Five-Sprint Deterministic Implementation Plan

### Sprint 1: Protocol Models, Validator, Conflict Engine & SQLite WAL Store

**Goal:** Build the pure domain core and crash-safe persistence layer with zero network dependencies.

**Files to Create:**

1. `backend/requirements.txt`:
`fastapi`, `uvicorn[standard]`, `pydantic`, `python-multipart`, `httpx`, `websockets`, `python-dotenv`, `pytest`, `pytest-asyncio`, `pillow`
2. `backend/app/__init__.py`
3. `backend/app/protocol.py`:
* All enums and Pydantic models from Section 1 (`AudioMessage`, `StopPlaybackMessage`, `TranscriptMessage`, `ChatMessage`, `InputOption`, `InputRequest`, `InputError`, `InputCancel`, `RecordMessage`, `InputResponse`, `RecordField`, `RecordConflict`, `EngagementMetrics`, `FinalRecord`).
* Internal `SessionState` model holding `session_id`, `revision`, `delivered_revision`, `outcome`, `outcome_reason`, `fields: dict[str, RecordField]`, `failure_counts: dict[str, int]`, `conflicts: list[RecordConflict]`, `open_request: InputRequest | None`, `email_prompted: bool`, `confirm_rejections: int`, `injury_review_flag: bool`, `vision_review_flag: bool`, `engagement: EngagementMetrics`, `cost_usd: float`, `latencies_ms: list[float]`, `conversation_history: list[dict]`, `created_at: str`, `updated_at: str`, `ended_at: str | None`.


4. `backend/app/validator.py`:
* `validate_field(field: str, raw_value: Any, source: FieldSource, now_iso: str | None = None, valid_upload_urls: set[str] | None = None) -> tuple[bool, Any, str | None]`
* `apply_field_update(state: SessionState, field: str, raw_value: Any, source: FieldSource, valid_upload_urls: set[str] | None = None) -> tuple[bool, str | None, InputCancel | None]`
* `next_missing_input_request(state: SessionState, call_active: bool) -> InputRequest | None`:
* When `call_active == True`: only queues chat-native cards (`policyNumber` -> `vehicleType` -> `email` -> `damagePhotos` -> `confirmSubmit` once spoken fields 1–5 are valid).
* When `call_active == False` (chat-only or post-call fallback): asks for any missing spoken field (1–5) first, then chat cards (6–9), then `confirmSubmit` (10).


* `evaluate_outcome(state: SessionState, force_partial: bool = False, opt_out_reason: str | None = None) -> FinalRecord | None`


5. `backend/app/store.py`:
* SQLite connection with `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;`.
* Tables:
* `sessions (session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, revision INTEGER NOT NULL, delivered_revision INTEGER NOT NULL, updated_at TEXT NOT NULL)`
* `events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, timestamp TEXT NOT NULL, channel TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL)`
* `uploads (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, filename TEXT NOT NULL, url TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, vision_ok INTEGER NOT NULL, created_at TEXT NOT NULL)`


* Methods: `get_or_create_session(session_id)`, `save_session(state)`, `append_event(session_id, channel, event_type, payload)`, `get_timeline(session_id)`, `register_upload(...)`, `get_session_upload_urls(session_id)`, `mark_revision_delivered(session_id, revision)`.


6. `backend/tests/test_sprint1.py`

**Sprint 1 Acceptance Gate (`pytest backend/tests/test_sprint1.py`):**

* Serialization test verifying `InputRequest`, `TranscriptMessage`, `AudioMessage`, and `RecordMessage` match `starter/src/protocol/types.ts` camelCase keys 1:1 and `InputRequest.pattern` is omitted.
* Unit tests for all 10 field validators (valid & invalid inputs, natural date parsing, spoken email `"john at example dot com"`, policy number regex).
* Conflict resolution test: spoken `policyNumber="POL-111111"` followed by chat `policyNumber="POL-222222"` resolves to `"POL-222222"` (`chatValue`) and populates `state.conflicts`.
* 3 consecutive invalid submissions on `policyNumber` transition session to `outcome="needsReview"` (`revision=1`).
* Valid completion with `injuries="whiplash in neck"` + `confirmSubmit=True` produces `outcome="needsReview"` with injury reason.
* Persistence test: save state to disk SQLite file, instantiate fresh `SessionStore` on same file, verify full recovery and idempotent `mark_revision_delivered`.

---

### Sprint 2: `/chat` WebSocket, File Uploads, Webhook Sink & Restart Recovery

**Goal:** Deliver a working `/chat` WebSocket server, validated image upload endpoint, idempotent webhook delivery, and headless HTTP test endpoints.

**Files to Create/Update:**

1. `backend/app/webhook.py`:
* `deliver_final_record(store: SessionStore, state: SessionState, record: FinalRecord) -> bool`:
* Checks `state.delivered_revision < record.revision`.
* Logs structured JSON banner to stdout (`EVENT=FINAL_RECORD_DELIVERED`) and appends to `events` table.
* POSTs JSON to `WEBHOOK_URL` (if configured) and internal `/api/webhook-sink`.
* Calls `store.mark_revision_delivered(state.session_id, record.revision)`.




2. `backend/app/session_manager.py`:
* Per-session `asyncio.Lock` (`self._locks[session_id]`) preventing concurrent mutation races between `/chat`, `/call`, and timers.
* Registry of active WebSockets (`chat_ws: dict[str, WebSocket]`, `call_ws: dict[str, WebSocket]`).
* `handle_chat_connect(session_id, ws)`: loads session; if `FinalRecord` already exists and outcome is terminal (`completed`, `optedOut`, `needsReview`), sends greeting + `RecordMessage`; otherwise sends greeting (if new) and re-emits `state.open_request` (or dispatches `next_missing_input_request`).
* `handle_chat_input(session_id, InputResponse)`: validates under lock, increments `engagement.chatMessages`, emits `InputError` on failure (or `FinalRecord` on 3rd strike), or persists valid field, notifies active call queue (if connected), and dispatches next `InputRequest` or `FinalRecord`.


3. `backend/app/main.py`:
* FastAPI app with `CORSMiddleware(allow_origins=["*"])`.
* `WS /chat` (`?sessionId=...`).
* `POST /sessions/{sessionId}/files`: verifies size `<= 10 MB` and PNG/JPEG/WEBP/GIF magic bytes (`\x89PNG`, `\xff\xd8\xff`, `RIFF....WEBP`, `GIF8`). Saves to `{UPLOAD_DIR}/{sessionId}/{uuid}_{safe_name}` and returns `{"url": "{base_url}/files/{sessionId}/{uuid}_{safe_name}"}`.
* `GET /files/{sessionId}/{filename}`: serves uploaded image file.
* `POST /api/sessions`, `POST /api/sessions/{sessionId}/input`, `GET /sessions/{sessionId}/timeline`, `GET /sessions/{sessionId}/telemetry`, `POST /api/webhook-sink`.


4. `backend/tests/test_sprint2.py`

**Sprint 2 Acceptance Gate (`pytest backend/tests/test_sprint2.py`):**

* Full chat-only E2E test over `TestClient.websocket_connect("/chat?sessionId=s_chat_e2e")`: completes all 8 fields + optional email + confirm, receives `RecordMessage` with `outcome="completed"`, `revision=1`.
* Error & 3-strike test over `/chat`: sends `"POL-123"`, receives `{"type": "inputError", "requestId": ...}`; repeats twice more and receives `{"type": "record", "record": {"outcome": "needsReview", ...}}`.
* File upload test: `POST /sessions/s_upload/files` with fake `.txt` renamed to `.jpg` returns HTTP 400; valid PNG bytes return HTTP 200 with working `url`, and `GET` on that `url` returns HTTP 200 image bytes.
* Server restart simulation: complete 4 fields on `/chat`, close WS, re-instantiate `SessionManager` against same SQLite DB, reconnect `/chat?sessionId=...`, verify remaining 4 fields + confirm complete cleanly without re-asking the first 4 fields, and verify webhook sink received `revision=1` once.

---

### Sprint 3: `/call` Full-Duplex Voice Pipeline (Deepgram STT -> Groq LLM -> Aura TTS) & Barge-In

**Goal:** Implement the real-time binary-in / JSON-out `/call` WebSocket with streaming STT, tool-calling LLM, streaming 24kHz PCM16 TTS, and `<50ms` internal barge-in cancellation.

**Files to Create/Update:**

1. `backend/app/voice.py`:
* **Injectable Provider Interfaces** (`STTProvider`, `LLMProvider`, `TTSProvider`) with production implementations (`DeepgramLiveSTT`, `GroqToolLLM`, `DeepgramAuraTTS`) and deterministic mock providers for offline `pytest` verification.
* **Audio Ingest:** Reads 640-byte PCM16 LE frames from `/call` WS and forwards to Deepgram Live WS (`model=nova-3&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&utterance_end_ms=1000&vad_events=true&endpointing=300`).
* **Transcript Forwarding:** Emits `TranscriptMessage(role="user", text=..., final=is_final, id=utterance_id)` over `/call`.
* **Immediate Barge-In Controller:**
* If `agent_speaking == True` and Deepgram emits `SpeechStarted` (with speech confirmation) or non-empty user transcript:
1. Immediately cancel active `llm_tts_task` (`asyncio.Task.cancel()`).
2. Send `{"type": "stopPlayback"}` over `/call` WS.
3. Log `barge_in` event to `store.py`.




* **LLM Agent & Tool Calling (`meta-llama/llama-4-scout-17b-16e-instruct`):**
* Dynamic system prompt injected with current `SessionState` summary (which fields are `valid`, which are `missing`, and what card is currently open in `/chat`).
* Tools exposed to LLM:
* `record_spoken_fields(incidentDescription?: str, incidentLocation?: str, incidentTime?: str, injuries?: str, vehicleDrivable?: bool, email?: str, policyNumber?: str)`
* `request_chat_card(field: Literal["policyNumber", "vehicleType", "damagePhotos", "email", "confirmSubmit"])`
* `opt_out_session(reason: str)`




* **TTS Synthesis (`aura-2-thalia-en` / `aura-asteria-en`, `encoding=linear16&sample_rate=24000&container=none`):**
* Streams 24,000 Hz PCM16 LE mono chunks encoded as base64 in `AudioMessage(sampleRate=24000, data=...)` alongside `TranscriptMessage(role="agent", text=..., final=False -> True, id=...)`.
* Records `t_user_speech_final` -> `t_first_audio_sent` latency (ms) and accumulates STT + LLM + TTS USD cost on every turn.




2. `backend/app/main.py` (wire `WS /call`).
3. `backend/tests/test_sprint3.py`

**Sprint 3 Acceptance Gate (`pytest backend/tests/test_sprint3.py`):**

* Connect `/call?sessionId=s_voice_1`, stream 640-byte binary PCM frames, verify `TranscriptMessage(role="user")` (`final=False` and `final=True`) and `AudioMessage(sampleRate=24000)` are emitted.
* Verify LLM tool execution (`record_spoken_fields`) validates and persists all 5 spoken fields in `store.py` with `source="call"` and dispatches the first chat card (`policyNumber`) on `/chat`.
* Barge-in test: while agent TTS chunks are streaming on `/call`, send user speech frame -> verify `{"type": "stopPlayback"}` is emitted immediately and no further stale TTS chunks from the cancelled turn are sent.
* Auto-hangup test: when session reaches terminal `completed` while `/call` is connected, server closes `/call` with `code=1000, reason="claim submitted"`.

---

### Sprint 4: Cross-Channel Synchronization, Call Resume, Partial Watchdog & Telemetry

**Goal:** Unify `/call` and `/chat` into a single state machine with cross-channel acknowledgments, `inputCancel`, call drop/resume continuity, and partial-to-completed revision upgrades.

**Files to Update:**

1. `backend/app/session_manager.py` & `backend/app/voice.py`:
* **Chat Input During Active Call (`chat -> call`):**
* When `handle_chat_input` validates a field while `/call` is connected, it pushes a `ChannelNotification(field, value, next_card)` into the voice session's async notification queue and dispatches the next chat card.
* If the voice agent is currently idle (`not user_speaking and not agent_speaking`), `voice.py` immediately speaks a concise acknowledgment (*"Got your policy number POL-123456. I've just sent the vehicle type selector to your chat."*) and streams TTS. If a voice turn is currently active, the notification remains in the session state and is spoken naturally at the end of the current turn without overlapping audio.


* **Spoken Answer Cancels Open Chat Card (`call -> chat` `inputCancel`):**
* If `state.open_request` is open in `/chat` (e.g., `field="email"` or `field="policyNumber"`) and `record_spoken_fields` validates that same field from `source="call"`, `session_manager` immediately sends `{"type": "inputCancel", "requestId": open_req.id, "reason": "given on the call"}` over `/chat`, clears `open_request`, and advances to the next missing chat card.


* **Call Drop / Hang-up -> Seamless Chat Continuation:**
* On `/call` disconnect (`WebSocketDisconnect`), `session_manager` updates `engagement.callSeconds`. If outcome is not yet terminal, it immediately sends `ChatMessage(role="agent", text="Call ended — we can finish the remaining details right here in chat.")` and dispatches `next_missing_input_request(state, call_active=False)` so any uncollected spoken or chat fields can be completed in `/chat`.


* **Call Reconnect / Resume from Chat (Bonus):**
* If the user clicks Call again on an existing `sessionId`, `voice.py` inspects `state.fields` on connect and greets the user with exact context (*"Welcome back! I already have your incident details and policy number. Let's finish the remaining items..."*) without repeating already-valid questions.


* **Inactivity Watchdog & Revision Upgrade (`revision: 1 -> 2`):**
* Background task checks sessions where `call_ws` is disconnected and `outcome` is None or `"partial"`.
* If `now - last_activity >= PARTIAL_TIMEOUT_SECONDS` and `outcome is None`: emits `FinalRecord(outcome="partial", revision=1)`.
* If the user subsequently submits remaining fields in `/chat` (or reconnects `/call`) and confirms `confirmSubmit=True`: transitions to `outcome="completed"` (or `"needsReview"`), increments `revision=2`, emits the new `RecordMessage`, and delivers `revision=2` via `webhook.py`.




2. `backend/tests/test_sprint4.py`

**Sprint 4 Acceptance Gate (`pytest backend/tests/test_sprint4.py`):**

* Cross-channel ack test: connect both `/chat` and `/call` on same `sessionId`; submit `policyNumber` on `/chat` -> verify `/call` emits agent `TranscriptMessage` & `AudioMessage` acknowledging `POL-123456` and `/chat` receives `vehicleType` `inputRequest`.
* `inputCancel` test: with `email` `inputRequest` (`id="req_email"`) open on `/chat`, speak email on `/call` -> verify `/chat` receives `{"type": "inputCancel", "requestId": "req_email", "reason": "given on the call"}` and `fields["email"].source == "call"`.
* Call drop & resume test: collect 2 spoken fields on `/call`, disconnect `/call`, verify `/chat` immediately dispatches `inputRequest` for the 3rd spoken field; answer it in `/chat`, then reconnect `/call` and verify the voice agent greeting references the 3 collected fields without re-asking them.
* Partial -> Completed revision upgrade test: trigger inactivity timeout (`PARTIAL_TIMEOUT_SECONDS=0.1` in test) -> verify `revision=1, outcome="partial"` delivered; submit remaining cards on `/chat` -> verify `revision=2, outcome="completed"` delivered.
* Telemetry & timeline test: verify `GET /sessions/{id}/telemetry` returns `p50_latency_ms`, `p95_latency_ms`, and itemized `cost_usd`, and `GET /sessions/{id}/timeline` returns chronological events across both channels.

---

### Sprint 5: Vision Upload Verification, Rate Limiting, Single-Container Docker & README

**Goal:** Add multimodal photo verification, public-URL rate limiting, single-container production build (`Node 22 -> Python 3.12`), and the complete `README.md`.

**Files to Create/Update:**

1. `backend/app/vision.py`:
* Inspects uploaded damage photo via Groq `meta-llama/llama-4-scout-17b-16e-instruct` (or deterministic local image inspection fallback when `GROQ_API_KEY` is absent/mocked) to verify whether the image depicts a vehicle/damage (`vision_ok: bool, reason: str`), adding vision token cost to `state.cost_usd`.


2. `backend/app/rate_limit.py`:
* In-memory sliding-window rate limiter middleware (default: max 30 new sessions/hour per IP, max 60 file uploads/hour per IP, max 10 minutes of active call audio per session) so public evaluators can test freely while protecting API credits.


3. `Dockerfile`:
* **Stage 1 (`node:22-alpine`):** Copies `starter/`, runs `npm ci && VITE_SERVER_URL="" npm run build` to produce `starter/dist`.
* **Stage 2 (`python:3.12-slim`):** Installs `backend/requirements.txt`, copies `backend/app` and `starter/dist`, mounts `/data` for SQLite WAL + uploads, and runs `uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1`.


4. `backend/app/main.py`:
* Mounts compiled `starter/dist` static assets at `/` (when directory exists) so `https://<deployed-host>/` serves the complete React UI and WebSockets from a single origin.


5. `README.md`:
* Covers all 10 required sections from `BRIEF.md`:
1. Architecture & cross-channel state sharing diagram.
2. Final record JSON Schema, validation, normalization & conflict resolution rules.
3. Outcome triggers (`completed`, `partial`, `optedOut`, `needsReview`), delivery, and revision lifecycle (`rev 1 -> rev 2`).
4. Stack choice & session cost explanation (**strictly <= 150 words**).
5. Product decisions made where the brief was open-ended.
6. Edge cases handled vs. known limitations.
7. Least-trusted component analysis.
8. Empirical voice latency (`p50` / `p95` ms) & itemized USD cost breakdown table.
9. Roadmap with another week.
10. AI coding assistant reflection (where AI helped vs. where it misled, such as client-side `pattern` pre-validation).




6. `backend/tests/test_sprint5.py`

**Sprint 5 Acceptance Gate (`pytest backend/tests/`):**

* Full test suite (`test_sprint1.py` through `test_sprint5.py`) passes 100%.
* Vision check test: verifies valid vehicle image passes and non-vehicle image increments failure / flags `needsReview`.
* Rate-limit middleware test: verifies HTTP 429 after exceeding configured threshold.