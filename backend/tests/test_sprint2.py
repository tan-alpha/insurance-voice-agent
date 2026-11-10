"""Sprint 2 acceptance-gate tests.

Covers backend/tests/test_sprint2.py acceptance criteria from @SPRINT_PLAN.md:
  a) Happy-path chat E2E: completed outcome, revision=1.
  b) 3-strike escalation on policyNumber -> needsReview.
  c) File upload validation (400 on fake image, 200 + GET round-trip on PNG).
  d) Server restart simulation: resume mid-session, no repeated fields, webhook idempotent.
"""

from __future__ import annotations

import json
import os
import tempfile

from fastapi.testclient import TestClient

from backend.app.main import app, store as _store, manager as _manager

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
    b"\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01"
    b"\x01\x00\x05\xdd\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _fresh_app_and_store(db_path: str):
    """Build a fresh TestClient against a new store/manager on the given DB."""
    from backend.app.store import SessionStore
    from backend.app.session_manager import SessionManager

    s = SessionStore(db_path)
    import backend.app.main as main_mod

    m = SessionManager(s, os.path.dirname(db_path) + "/uploads", sink=main_mod._webhook_sink)
    # Rebind module singletons.
    main_mod.store = s
    main_mod.manager = m
    main_mod._webhook_sink.clear()
    return TestClient(app)


def _recv(ws) -> dict:
    return json.loads(ws.receive_text())


def _send_input(ws, request_id: str, field: str, value) -> None:
    ws.send_text(
        json.dumps(
            {"type": "inputResponse", "requestId": request_id, "field": field, "value": value}
        )
    )


# ---------------------------------------------------------------------------
# (a) Happy-path chat E2E
# ---------------------------------------------------------------------------

def test_happy_path_chat_completed():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        client = _fresh_app_and_store(db_path)

        # Pre-upload a valid damage photo so damagePhotos validates in chat.
        up = client.post(
            "/sessions/s_e2e/files",
            files={"file": ("damage.png", PNG_BYTES, "image/png")},
        )
        assert up.status_code == 200, up.text
        photo_url = up.json()["url"]

        with client.websocket_connect("/chat?sessionId=s_e2e") as ws:
            # Greeting
            msg = _recv(ws)
            assert msg["type"] == "message"

            # 1. incidentDescription
            msg = _recv(ws)
            assert msg["type"] == "inputRequest"
            assert msg["field"] == "incidentDescription"
            _send_input(ws, msg["id"], "incidentDescription", "Rear-ended by a truck at a signal")

            # 2. incidentLocation
            msg = _recv(ws)
            assert msg["field"] == "incidentLocation"
            _send_input(ws, msg["id"], "incidentLocation", "MG Road")

            # 3. incidentTime
            msg = _recv(ws)
            assert msg["field"] == "incidentTime"
            _send_input(ws, msg["id"], "incidentTime", "2 hours ago")

            # 4. injuries
            msg = _recv(ws)
            assert msg["field"] == "injuries"
            _send_input(ws, msg["id"], "injuries", "none")

            # 5. vehicleDrivable
            msg = _recv(ws)
            assert msg["field"] == "vehicleDrivable"
            _send_input(ws, msg["id"], "vehicleDrivable", "yes")

            # 6. policyNumber
            msg = _recv(ws)
            assert msg["field"] == "policyNumber"
            _send_input(ws, msg["id"], "policyNumber", "POL-123456")

            # 7. vehicleType
            msg = _recv(ws)
            assert msg["field"] == "vehicleType"
            _send_input(ws, msg["id"], "vehicleType", "car")

            # 8. email (optional) -> skip
            msg = _recv(ws)
            assert msg["field"] == "email"
            _send_input(ws, msg["id"], "email", "skip")

            # 9. damagePhotos -> use pre-uploaded URL
            msg = _recv(ws)
            assert msg["field"] == "damagePhotos"
            _send_input(ws, msg["id"], "damagePhotos", [photo_url])

            # 10. confirmSubmit
            msg = _recv(ws)
            assert msg["field"] == "confirmSubmit"
            _send_input(ws, msg["id"], "confirmSubmit", True)

            # Terminal record
            msg = _recv(ws)
            assert msg["type"] == "record"
            record = msg["record"]
            assert record["sessionId"] == "s_e2e"
            assert record["revision"] == 1
            assert record["outcome"] == "completed"
            assert "All required claim fields validated" in record["outcomeReason"]
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# (b) 3-strike escalation on policyNumber
# ---------------------------------------------------------------------------

def test_three_strike_policyNumber_needsReview():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        client = _fresh_app_and_store(db_path)

        with client.websocket_connect("/chat?sessionId=s_strike") as ws:
            # Consume greeting + first request (incidentDescription).
            _recv(ws)
            msg = _recv(ws)
            assert msg["field"] == "incidentDescription"

            # Fill spoken fields 1-5 quickly to reach policyNumber.
            _send_input(ws, msg["id"], "incidentDescription", "Rear-ended by a truck at a signal")
            msg = _recv(ws); _send_input(ws, msg["id"], "incidentLocation", "MG Road")
            msg = _recv(ws); _send_input(ws, msg["id"], "incidentTime", "2 hours ago")
            msg = _recv(ws); _send_input(ws, msg["id"], "injuries", "none")
            msg = _recv(ws); _send_input(ws, msg["id"], "vehicleDrivable", "yes")

            # Now policyNumber is the open card. Send 3 invalid values.
            errors = 0
            final = None
            # First get the policyNumber prompt.
            msg = _recv(ws)
            assert msg["field"] == "policyNumber"
            req_id = msg["id"]
            for _ in range(3):
                _send_input(ws, req_id, "policyNumber", "not-a-policy")

            # Collect responses: 2 InputErrors + 1 RecordMessage (needsReview).
            for _ in range(3):
                resp = _recv(ws)
                if resp["type"] == "inputError":
                    errors += 1
                elif resp["type"] == "record":
                    final = resp
                    break

            assert errors == 2, f"expected 2 InputError, got {errors}"
            assert final is not None, "expected terminal record on 3rd strike"
            assert final["type"] == "record"
            assert final["record"]["outcome"] == "needsReview"
            assert final["record"]["revision"] == 1
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# (c) File upload validation
# ---------------------------------------------------------------------------

def test_file_upload_validation():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        client = _fresh_app_and_store(db_path)

        # Fake image: .txt renamed to .jpg -> HTTP 400
        resp = client.post(
            "/sessions/s_upload/files",
            files={"file": ("fake.jpg", b"this is not an image", "image/jpeg")},
        )
        assert resp.status_code == 400

        # Valid PNG -> HTTP 200 with url
        resp = client.post(
            "/sessions/s_upload/files",
            files={"file": ("damage.png", PNG_BYTES, "image/png")},
        )
        assert resp.status_code == 200, resp.text
        url = resp.json()["url"]
        assert "/files/s_upload/" in url

        # GET on that URL returns identical bytes
        get_resp = client.get(url)
        assert get_resp.status_code == 200
        assert get_resp.content == PNG_BYTES
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# (d) Server restart simulation
# ---------------------------------------------------------------------------

def test_server_restart_resume_no_repeat():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # ---- Phase 1: fill 4 fields, then disconnect ----
        client = _fresh_app_and_store(db_path)
        with client.websocket_connect("/chat?sessionId=s_restart") as ws:
            _recv(ws)  # greeting
            steps = [
                ("incidentDescription", "Rear-ended by a truck at a signal"),
                ("incidentLocation", "MG Road"),
                ("incidentTime", "2 hours ago"),
                ("injuries", "none"),
            ]
            for field, value in steps:
                msg = _recv(ws)
                assert msg["field"] == field, f"expected {field}, got {msg.get('field')}"
                _send_input(ws, msg["id"], field, value)
        # Disconnect (exit context). Simulate server restart.

        # ---- Phase 2: new manager/store against same DB ----
        client2 = _fresh_app_and_store(db_path)
        with client2.websocket_connect("/chat?sessionId=s_restart") as ws:
            # Should NOT re-send greeting or repeat fields 1-4.
            # Next prompt should be vehicleDrivable (field 5).
            msg = _recv(ws)
            assert msg["type"] == "inputRequest"
            assert msg["field"] == "vehicleDrivable", f"expected vehicleDrivable, got {msg.get('field')}"

            _send_input(ws, msg["id"], "vehicleDrivable", "yes")

            # Continue remaining fields.
            msg = _recv(ws); assert msg["field"] == "policyNumber"
            _send_input(ws, msg["id"], "policyNumber", "POL-123456")

            msg = _recv(ws); assert msg["field"] == "vehicleType"
            _send_input(ws, msg["id"], "vehicleType", "car")

            msg = _recv(ws); assert msg["field"] == "email"
            _send_input(ws, msg["id"], "email", "skip")

            # Upload a photo for damagePhotos.
            up = client2.post(
                "/sessions/s_restart/files",
                files={"file": ("damage.png", PNG_BYTES, "image/png")},
            )
            assert up.status_code == 200
            photo_url = up.json()["url"]

            msg = _recv(ws); assert msg["field"] == "damagePhotos"
            _send_input(ws, msg["id"], "damagePhotos", [photo_url])

            msg = _recv(ws); assert msg["field"] == "confirmSubmit"
            _send_input(ws, msg["id"], "confirmSubmit", True)

            # Terminal record.
            msg = _recv(ws)
            assert msg["type"] == "record"
            record = msg["record"]
            assert record["revision"] == 1
            assert record["outcome"] == "completed"

        # ---- Phase 3: webhook received revision=1 exactly once ----
        sink = client2.get("/api/webhook-sink").json()
        deliveries = [r for r in sink["records"] if r.get("revision") == 1]
        assert len(deliveries) == 1, f"expected 1 webhook delivery of rev 1, got {len(deliveries)}"
    finally:
        os.unlink(db_path)
