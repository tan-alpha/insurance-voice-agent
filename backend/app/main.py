"""FastAPI application: /chat, /call, file uploads, and test endpoints (Sprint 2)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .protocol import InputResponse
from .session_manager import SessionManager
from .store import SessionStore

# --------------------------------------------------------------------------- config

DB_PATH = os.getenv("DB_PATH", "./data/sessions.db")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./data/uploads")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Image magic-byte signatures.
MAGIC = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF8": "image/gif",
}
WEBP_PREFIX = b"RIFF"
WEBP_MARK = b"WEBP"

# --------------------------------------------------------------------------- app + singletons

@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start_watchdog()
    yield
    await manager.stop_watchdog()

app = FastAPI(title="Voice + Chat Insurance Claim Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

store = SessionStore(DB_PATH)

# In-memory webhook sink for testing.
_webhook_sink: list[dict[str, Any]] = []

manager = SessionManager(store, UPLOAD_DIR, sink=_webhook_sink)


def _ensure_upload_dir(session_id: str) -> Path:
    d = Path(UPLOAD_DIR) / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _detect_mime(data: bytes) -> Optional[str]:
    for sig, mime in MAGIC.items():
        if data.startswith(sig):
            return mime
    if data.startswith(WEBP_PREFIX) and WEBP_MARK in data[8:12]:
        return "image/webp"
    return None


# --------------------------------------------------------------------------- sessions

@app.post("/api/sessions")
async def create_session() -> JSONResponse:
    session_id = str(uuid.uuid4())
    store.get_or_create_session(session_id)
    return JSONResponse({"session_id": session_id})


# --------------------------------------------------------------------------- chat websocket

@app.websocket("/chat")
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    session_id = ws.query_params.get("sessionId", "")
    if not session_id:
        await ws.close(code=1008, reason="sessionId required")
        return
    await manager.handle_chat_connect(session_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            await manager.handle_chat_message(session_id, raw)
    except WebSocketDisconnect:
        await manager.handle_chat_disconnect(session_id, ws)


# --------------------------------------------------------------------------- call websocket (voice pipeline)

@app.websocket("/call")
async def call_ws(ws: WebSocket) -> None:
    await ws.accept()
    session_id = ws.query_params.get("sessionId", "")
    if not session_id:
        await ws.close(code=1008, reason="sessionId required")
        return
    await manager.handle_call_connect(session_id, ws)


# --------------------------------------------------------------------------- file upload

@app.post("/sessions/{session_id}/files")
async def upload_file(session_id: str, file: UploadFile = File(...)) -> JSONResponse:
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="file exceeds 10 MB")

    mime = _detect_mime(data)
    if mime is None:
        raise HTTPException(status_code=400, detail="not a valid PNG/JPEG/WEBP/GIF image")

    safe_name = "".join(c for c in (file.filename or "upload") if c.isalnum() or c in "._-") or "upload"
    stored_name = f"{uuid.uuid4()}_{safe_name}"
    dest = _ensure_upload_dir(session_id) / stored_name
    dest.write_bytes(data)

    url = f"{BASE_URL}/files/{session_id}/{stored_name}"
    store.register_upload(
        session_id=session_id,
        filename=safe_name,
        url=url,
        mime_type=mime,
        size_bytes=len(data),
        vision_ok=True,
    )
    return JSONResponse({"url": url})


@app.get("/files/{session_id}/{filename}")
async def get_file(session_id: str, filename: str) -> FileResponse:
    path = Path(UPLOAD_DIR) / session_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path)


# --------------------------------------------------------------------------- webhook sink (test)

@app.post("/api/webhook-sink")
async def webhook_sink_post(payload: dict[str, Any]) -> JSONResponse:
    _webhook_sink.append(payload)
    return JSONResponse({"received": True})


@app.get("/api/webhook-sink")
async def webhook_sink_get() -> JSONResponse:
    return JSONResponse({"count": len(_webhook_sink), "records": _webhook_sink})


@app.delete("/api/webhook-sink")
async def webhook_sink_delete() -> JSONResponse:
    _webhook_sink.clear()
    return JSONResponse({"cleared": True})


# --------------------------------------------------------------------------- timeline / telemetry

@app.get("/sessions/{session_id}/timeline")
async def get_timeline(session_id: str) -> JSONResponse:
    return JSONResponse(store.get_timeline(session_id))


@app.get("/sessions/{session_id}/telemetry")
async def get_telemetry(session_id: str) -> JSONResponse:
    state = store.get_or_create_session(session_id)
    latencies = sorted(state.latencies_ms)
    p50 = p95 = 0.0
    if latencies:
        p50 = latencies[int(len(latencies) * 0.5) - 1] if len(latencies) > 1 else latencies[0]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95) - 1)]
    return JSONResponse(
        {
            "sessionId": session_id,
            "p50_latency_ms": p50,
            "p95_latency_ms": p95,
            "cost_usd": state.cost_usd,
            "calls": state.engagement.calls,
            "call_seconds": state.engagement.call_seconds,
            "chat_messages": state.engagement.chat_messages,
        }
    )
